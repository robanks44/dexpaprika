"""Settings — env-first config, Decimal money fields, list parsing."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from dexpaprika.config import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient DEXPAPRIKA_* env or real .env can leak into tests."""
    import os

    for key in list(os.environ):
        if key.startswith("DEXPAPRIKA_"):
            monkeypatch.delenv(key)


def test_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # keep ./data resolution inside the sandbox
    s = Settings.load()
    assert s.data_dir == Path("data")
    assert s.log_level == "INFO"
    assert s.secret_backend == "auto"  # noqa: S105 — backend selector, not a password
    assert s.dexpaprika_base_url == "https://api.dexpaprika.com"
    # S9 disabled by default: zero limits, no markets.
    assert s.max_position_usd == Decimal("0")
    assert s.max_delta_per_run_usd == Decimal("0")
    assert s.max_daily_adjustments == 0
    assert s.allowed_markets == []


def test_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEXPAPRIKA_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("DEXPAPRIKA_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")
    s = Settings.load()
    assert s.data_dir == tmp_path / "d"
    assert s.log_level == "DEBUG"
    assert s.secret_backend == "env"  # noqa: S105 — backend selector, not a password


def test_money_fields_are_decimal_from_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEXPAPRIKA_MAX_POSITION_USD", "13155.76")
    s = Settings.load()
    assert s.max_position_usd == Decimal("13155.76")
    assert isinstance(s.max_position_usd, Decimal)
    assert not isinstance(s.max_position_usd, float)


def test_comma_separated_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DEXPAPRIKA_BASE_RPC_URLS",
        "https://base-rpc.publicnode.com, https://base.llamarpc.com",
    )
    monkeypatch.setenv("DEXPAPRIKA_ALLOWED_MARKETS", "ETH/USD")
    s = Settings.load()
    assert s.base_rpc_urls == [
        "https://base-rpc.publicnode.com",
        "https://base.llamarpc.com",
    ]
    assert s.allowed_markets == ["ETH/USD"]


def test_rpc_urls_must_be_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEXPAPRIKA_BASE_RPC_URLS", "http://insecure.example.com")
    with pytest.raises(Exception, match="[Hh]ttps|URL"):
        Settings.load()


def test_invalid_log_level_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEXPAPRIKA_LOG_LEVEL", "LOUD")
    with pytest.raises(ValueError, match="LOG_LEVEL|log_level"):
        Settings.load()


def test_invalid_secret_backend_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "vault")
    with pytest.raises(ValueError, match="SECRET_BACKEND|secret_backend"):
        Settings.load()
