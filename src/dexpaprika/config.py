"""Env-first typed configuration (ENGINEERING_STANDARDS §6: 12-factor).

All settings come from ``DEXPAPRIKA_*`` environment variables, with an
optional ``.env`` file for NON-secret local development values only.
Secrets never live here — see :mod:`dexpaprika.secrets`.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Comma-separated in env (NoDecode stops pydantic-settings' JSON parsing so the
# raw string reaches our before-validator).
CommaList = Annotated[list[str], NoDecode]

_LIST_FIELDS = ("base_rpc_urls", "arbitrum_rpc_urls", "gmx_rest_peers", "allowed_markets")
_HTTPS_LIST_FIELDS = ("base_rpc_urls", "arbitrum_rpc_urls", "gmx_rest_peers")
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

    # --- S9 hard limits (Decimal money; zero/empty = execution disabled) ---
    max_position_usd: Decimal = Decimal("0")
    max_delta_per_run_usd: Decimal = Decimal("0")
    max_daily_adjustments: int = 0
    allowed_markets: CommaList = []

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

    @classmethod
    def load(cls) -> Settings:
        """Load settings from the environment (and optional .env)."""
        return cls()
