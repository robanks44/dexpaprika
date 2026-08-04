"""Env-first typed configuration (ENGINEERING_STANDARDS §6: 12-factor).

All settings come from ``DEXPAPRIKA_*`` environment variables, with an
optional ``.env`` file for NON-secret local development values only.
Secrets never live here — see :mod:`dexpaprika.secrets`.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Annotated, ClassVar, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Comma-separated in env (NoDecode stops pydantic-settings' JSON parsing so the
# raw string reaches our before-validator).
CommaList = Annotated[list[str], NoDecode]

_LIST_FIELDS = (
    "base_rpc_urls",
    "arbitrum_rpc_urls",
    "gmx_rest_peers",
    "btc_esplora_peers",
    "allowed_markets",
)
_HTTPS_LIST_FIELDS = ("base_rpc_urls", "arbitrum_rpc_urls", "gmx_rest_peers", "btc_esplora_peers")
_HTTPS_SCALAR_FIELDS = ("dexpaprika_base_url", "ntfy_server")


class Settings(BaseSettings):
    """Application settings; construct via :meth:`load`."""

    model_config = SettingsConfigDict(
        env_prefix="DEXPAPRIKA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- core ---
    data_dir: Path = Path("data")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    secret_backend: Literal["auto", "keyring", "env"] = "auto"  # noqa: S105 — backend selector, not a password

    # --- providers (endpoint lists are config, not code) ---
    base_rpc_urls: CommaList = [
        "https://base-rpc.publicnode.com",
        "https://base.llamarpc.com",
        "https://mainnet.base.org",
    ]
    arbitrum_rpc_urls: CommaList = [
        "https://arb1.arbitrum.io/rpc",
        "https://arbitrum-one-rpc.publicnode.com",
        "https://arbitrum.llamarpc.com",
    ]
    gmx_rest_peers: CommaList = [
        "https://arbitrum.gmxapi.io/v1",
        "https://arbitrum.gmxapi.ai/v1",
    ]
    # Native-BTC holdings (S5.5): Esplora primary, mempool.space fallback.
    btc_esplora_peers: CommaList = [
        "https://blockstream.info/api",
        "https://mempool.space/api",
    ]
    # SlipStream NFPM registry (>=2 deployments exist on Base — never hardcode one)
    base_nfpm_addresses: CommaList = [
        "0x827922686190790b37229fd06084350E74485b72",
        "0xa990c6a764b73bf43cee5bb40339c3322fb9d55f",
    ]
    # Pools checked on the CLGauge staked path (gauge custody).
    base_tracked_pools: CommaList = ["0x56aeaf4af2df4bdfd9d865830fefdd278b25e7ef"]
    # Aave v3 Pool on Base (probe-verified 2026-08-02 via getUserAccountData).
    aave_pool_base: str = "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"
    dexpaprika_base_url: str = "https://api.dexpaprika.com"
    ntfy_server: str = "https://ntfy.sh"

    # Rebalance band (fraction of max delta) before a hedge adjustment is flagged.
    hedge_rebalance_band: Decimal = Decimal("0.075")

    # --- alerts (S8) ---
    # Same rule fired within this window is suppressed (ntfy free-tier etiquette:
    # alert per state-change, never per poll tick).
    alert_cooldown_minutes: int = 60
    # Hourly recorder + slack; older newest-snapshot means the pipeline is down.
    snapshot_staleness_minutes: int = 90
    # Monthly credit budgets only — rate windows fill transiently by design.
    quota_alert_used_pct: Decimal = Decimal("0.80")

    # --- scheduler (S11, container/VPS path) ---
    # Alerts-monitor cadence; snapshot stays hourly-on-the-hour, backup daily.
    scheduler_alerts_minutes: int = 5

    # --- S13 external watchdog + daily digest ---
    # Heartbeat cadence to the off-machine dead-man's switch. Keep well under the
    # switch's grace period so a single missed ping does not trip it.
    watchdog_heartbeat_minutes: int = 5
    # Newest-snapshot age past which the machine is judged stale → pings `fail`.
    watchdog_stale_minutes: int = 15
    # Hour (UTC) of the daily "all is well" digest to ntfy.
    watchdog_digest_hour: int = 13

    # --- S9 hard limits (Richard, 2026-08-02; zero/empty = that limit disabled) ---
    max_position_usd: Decimal = Decimal("20000")
    max_delta_per_run_usd: Decimal = Decimal("5000")
    max_daily_adjustments: int = 4
    allowed_markets: CommaList = ["ETH/USD"]
    # Execution plumbing (S9): two-step arming with expiry, an order-submission
    # rate limit independent of the venue's, and the approval-poll timeout.
    arm_ttl_minutes: int = 30
    order_rate_limit_seconds: int = 60
    approval_timeout_minutes: int = 10
    # Execution target (S9.5): where GMX orders go. Default Arbitrum One (mainnet);
    # set 421614 for the Arbitrum Sepolia testnet rehearsal. GMX runs ONLY on these
    # chains — Base is not a GMX venue (verified 2026-08), so it is rejected.
    gmx_chain_id: int = 42161
    execution_account: str = "0xC155A616e39D7B83E37e8FD9d2106E1BC056d7Fe"

    @field_validator(*_LIST_FIELDS, mode="before")
    @classmethod
    def _parse_comma_separated(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator(*_HTTPS_LIST_FIELDS)
    @classmethod
    def _require_https_urls(cls, value: list[str]) -> list[str]:
        for url in value:
            if not url.startswith("https://"):
                msg = f"URL {url!r} must use HTTPS (ENGINEERING_STANDARDS §2)"
                raise ValueError(msg)
        return value

    @field_validator(*_HTTPS_SCALAR_FIELDS)
    @classmethod
    def _require_https_url(cls, value: str) -> str:
        if not value.startswith("https://"):
            msg = f"URL {value!r} must use HTTPS (ENGINEERING_STANDARDS §2)"
            raise ValueError(msg)
        return value

    # Chains GMX v2 deploys perpetuals + subaccounts + order routing on.
    # Arbitrum One (42161) / Sepolia (421614), Avalanche C-Chain (43114).
    _GMX_CHAIN_IDS: ClassVar[frozenset[int]] = frozenset({42161, 421614, 43114})

    @field_validator("gmx_chain_id")
    @classmethod
    def _known_gmx_chain(cls, value: int) -> int:
        if value not in cls._GMX_CHAIN_IDS:
            msg = (
                f"gmx_chain_id {value} is not a GMX venue — GMX runs on Arbitrum"
                " (42161/421614) and Avalanche (43114) only; Base is not a GMX chain"
            )
            raise ValueError(msg)
        return value

    @classmethod
    def load(cls) -> Settings:
        """Load settings from the environment (and optional .env)."""
        return cls()
