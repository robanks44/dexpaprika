"""Read-only LIVE smoke suite (S10) — EXCLUDED from the offline gate.

Run: ``make smoke`` (real network, throwaway data dir, records only to its
own temp DB, sends nothing). This is the live leg of the LOOP_PROMPT Step 8
whole-system check. Requires DEXPAPRIKA_SECRET_NTFY_TOPIC (or keyring) only
for the healthcheck secrets check — nothing is published.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dexpaprika.cli import EXIT_OK, main

pytestmark = pytest.mark.live

WALLET = "0xC155A616e39D7B83E37e8FD9d2106E1BC056d7Fe"

# conftest.py wipes DEXPAPRIKA_* per test (offline-gate determinism). The live
# suite is the one place ambient secret config is WANTED — capture it at import
# time (before fixtures run) and restore it per test.
_AMBIENT_SECRETS = {
    key: value
    for key, value in os.environ.items()
    if key == "DEXPAPRIKA_SECRET_BACKEND" or key.startswith("DEXPAPRIKA_SECRET_")
}


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("smoke-data")


@pytest.fixture(autouse=True)
def _env(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEXPAPRIKA_DATA_DIR", str(data_dir))
    for key, value in _AMBIENT_SECRETS.items():
        monkeypatch.setenv(key, value)


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_01_bootstrap_and_snapshot(capsys: pytest.CaptureFixture[str]) -> None:
    code, _ = run_json(capsys, "db", "migrate")
    assert code == EXIT_OK
    code, _ = run_json(capsys, "wallets", "add", "--chain", "evm", "--address", WALLET)
    assert code == EXIT_OK
    code, out = run_json(capsys, "snapshot")
    assert code == EXIT_OK
    recorded = out["recorded"]
    assert isinstance(recorded, dict)
    assert set(recorded) == {"lp", "hedge", "defi", "holdings"}


def test_02_report_and_hedge(capsys: pytest.CaptureFixture[str]) -> None:
    code, report = run_json(capsys, "report")
    assert code == EXIT_OK
    assert isinstance(report["totals_usd"], dict)
    code, hedge = run_json(capsys, "hedge", "status")
    assert code == EXIT_OK
    analysis = hedge["analysis"]
    assert isinstance(analysis, dict)
    assert "quadrant" in analysis


def test_03_alerts_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run_json(capsys, "alerts", "check", "--dry-run")
    assert code == EXIT_OK
    assert isinstance(out["alerts"], list)


def test_04_healthcheck_all_pass(capsys: pytest.CaptureFixture[str]) -> None:
    code, health = run_json(capsys, "healthcheck")
    checks = health["checks"]
    assert isinstance(checks, dict)
    failing = {k: v for k, v in checks.items() if not str(v).startswith("ok")}
    assert code == EXIT_OK, json.dumps(failing, indent=2)
    assert not failing, json.dumps(failing, indent=2)
