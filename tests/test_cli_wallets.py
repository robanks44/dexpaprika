"""CLI wallets group + upgraded status/healthcheck — end-to-end through main()."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dexpaprika.cli import EXIT_DEGRADED, EXIT_FAILURE, EXIT_OK, main

EVM = "0xC155A616e39D7B83E37e8FD9d2106E1BC056d7Fe"
BTC = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import os

    for key in list(os.environ):
        if key.startswith("DEXPAPRIKA_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("DEXPAPRIKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_wallets_add_and_list(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run_json(
        capsys, "wallets", "add", "--chain", "evm", "--address", EVM.lower(), "--label", "main"
    )
    assert code == EXIT_OK
    added = out["wallet"]
    assert isinstance(added, dict)
    assert added["address"] == EVM  # normalized to EIP-55

    code, out = run_json(capsys, "wallets", "list")
    assert code == EXIT_OK
    wallets = out["wallets"]
    assert isinstance(wallets, list)
    assert len(wallets) == 1


def test_wallets_add_invalid_address_fails_cleanly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out = run_json(capsys, "wallets", "add", "--chain", "evm", "--address", "0xdead")
    assert code == EXIT_FAILURE
    assert "error" in out


def test_wallets_add_duplicate_fails(capsys: pytest.CaptureFixture[str]) -> None:
    run_json(capsys, "wallets", "add", "--chain", "evm", "--address", EVM)
    code, out = run_json(capsys, "wallets", "add", "--chain", "evm", "--address", EVM.lower())
    assert code == EXIT_FAILURE
    assert "error" in out


def test_wallets_exclude_include_remove(capsys: pytest.CaptureFixture[str]) -> None:
    run_json(capsys, "wallets", "add", "--chain", "btc", "--address", BTC, "--label", "cold")

    code, out = run_json(capsys, "wallets", "exclude", "--label", "cold")
    assert code == EXIT_OK
    wallet = out["wallet"]
    assert isinstance(wallet, dict)
    assert wallet["included"] is False

    code, out = run_json(capsys, "wallets", "include", "--label", "cold")
    assert code == EXIT_OK

    code, out = run_json(capsys, "wallets", "remove", "--address", BTC)
    assert code == EXIT_OK
    code, out = run_json(capsys, "wallets", "list")
    assert out["wallets"] == []


def test_wallets_remove_unknown_fails(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run_json(capsys, "wallets", "remove", "--address", EVM)
    assert code == EXIT_FAILURE
    assert "error" in out


def test_status_reports_wallet_counts_and_no_secrets(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEXPAPRIKA_SECRET_NTFY_TOPIC", "topic-value-must-not-leak")
    run_json(capsys, "wallets", "add", "--chain", "evm", "--address", EVM)
    run_json(capsys, "wallets", "exclude", "--address", EVM)

    code = main(["status", "--json"])
    raw = capsys.readouterr().out
    out = json.loads(raw)
    assert code == EXIT_OK
    wallets = out["wallets"]
    assert isinstance(wallets, dict)
    assert wallets["total"] == 1
    assert wallets["included"] == 0
    assert "topic-value-must-not-leak" not in raw


def test_healthcheck_secrets_present_ok(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEXPAPRIKA_SECRET_NTFY_TOPIC", "dummy")
    code = main(["healthcheck", "--json"])
    out = json.loads(capsys.readouterr().out)
    checks = out["checks"]
    assert isinstance(checks, dict)
    assert checks["secrets_present"] == "ok"
    assert checks["data_dir_writable"] == "ok"
    # Other checks still pending → overall degraded, exit 3 (standards §2).
    assert code == EXIT_DEGRADED
    assert out["healthy"] is False


def test_healthcheck_missing_secret_fails_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["healthcheck", "--json"])
    out = json.loads(capsys.readouterr().out)
    checks = out["checks"]
    assert isinstance(checks, dict)
    assert str(checks["secrets_present"]).startswith("fail")
    assert code == EXIT_DEGRADED


def test_healthcheck_never_leaks_secret_values(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEXPAPRIKA_SECRET_NTFY_TOPIC", "topic-value-must-not-leak")
    main(["healthcheck", "--json"])
    assert "topic-value-must-not-leak" not in capsys.readouterr().out
