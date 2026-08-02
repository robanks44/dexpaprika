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
    ]
    arbitrum_rpc_urls: CommaList = ["https://arb1.arbitrum.io/rpc"]
    gmx_rest_peers: CommaList = [
        "https://arbitrum.gmxapi.io/v1",
        "https://arbitrum.gmxapi.ai/v1",
    ]
    dexpaprika_base_url: str = "https://api.dexpaprika.com"
    ntfy_server: str = "https://ntfy.sh"

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
