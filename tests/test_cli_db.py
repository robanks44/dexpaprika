"""CLI db group + healthcheck DB checks — end-to-end through main()."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dexpaprika.cli import EXIT_DEGRADED, EXIT_FAILURE, EXIT_OK, main


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEXPAPRIKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_db_status_before_migrate(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run_json(capsys, "db", "status")
    assert code == EXIT_OK
    assert out["exists"] is False
    assert out["pending"]  # at least the initial migration is pending


def test_db_migrate_then_status(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run_json(capsys, "db", "migrate")
    assert code == EXIT_OK
    applied = out["applied"]
    assert isinstance(applied, list)
    assert applied  # names of migrations applied

    code, out = run_json(capsys, "db", "migrate")
    assert code == EXIT_OK
    assert out["applied"] == []  # idempotent

    code, out = run_json(capsys, "db", "status")
    assert code == EXIT_OK
    assert out["exists"] is True
    assert out["pending"] == []
    assert out["integrity"] == "ok"


def test_db_backup_and_restore(capsys: pytest.CaptureFixture[str]) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(capsys, "db", "backup")
    assert code == EXIT_OK
    backup_path = out["backup"]
    assert isinstance(backup_path, str)
    assert Path(backup_path).exists()

    code, out = run_json(capsys, "db", "restore", "--from", backup_path)
    assert code == EXIT_OK
    assert out["restored"] is True


def test_db_restore_without_backups_fails_cleanly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(capsys, "db", "restore")
    assert code == EXIT_FAILURE
    assert "error" in out


def test_healthcheck_db_checks(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEXPAPRIKA_SECRET_NTFY_TOPIC", "dummy")  # pragma: allowlist secret
    # This test is about the DB checks; the network pair is covered (mocked and
    # drilled) in test_integration.py.
    monkeypatch.setattr("dexpaprika.cli._check_network_health", lambda _s: ("ok", "ok"))
    # Before migrate: both DB checks fail with actionable messages.
    code = main(["healthcheck", "--json"])
    out = json.loads(capsys.readouterr().out)
    checks = out["checks"]
    assert isinstance(checks, dict)
    assert str(checks["db_integrity"]).startswith("fail")
    assert str(checks["migrations_current"]).startswith("fail")
    assert code == EXIT_DEGRADED

    # After migrate: both ok (no snapshots yet → last_snapshot_age keeps it
    # degraded — correctly).
    main(["db", "migrate", "--json"])
    capsys.readouterr()
    code = main(["healthcheck", "--json"])
    out = json.loads(capsys.readouterr().out)
    checks = out["checks"]
    assert isinstance(checks, dict)
    assert checks["db_integrity"] == "ok"
    assert checks["migrations_current"] == "ok"
    assert str(checks["last_snapshot_age"]).startswith("fail")
    assert code == EXIT_DEGRADED
