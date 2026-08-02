"""Gate-suite tests for the core CLI contract (status/healthcheck/exit codes).

Offline by construction (pytest-socket disables the network for the whole
suite via addopts). These tests pin the CLI contract the agent relies on:
JSON output, exit codes, honest degraded healthcheck until every check passes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dexpaprika import __version__
from dexpaprika.cli import (
    EXIT_DEGRADED,
    EXIT_OK,
    EXIT_USAGE,
    main,
)


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """status/healthcheck read Settings since S1 — point them at a tmp dir."""
    monkeypatch.setenv("DEXPAPRIKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")


def test_status_json_contract(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["status", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_OK
    assert payload["app"] == "dexpaprika"
    assert payload["version"] == __version__
    assert "s0" in payload["sections_complete"]


def test_status_human_output(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["status"])
    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "dexpaprika" in out
    # Human mode must not be JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_healthcheck_degraded_while_any_check_unimplemented(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["healthcheck", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_DEGRADED
    assert payload["healthy"] is False
    assert payload["degraded"] is True
    # Exit 0 only if ALL pass (ENGINEERING_STANDARDS §2): as long as any
    # check reports not-implemented, healthcheck must stay degraded.
    assert "not-implemented" in set(payload["checks"].values())


def test_healthcheck_covers_required_checks(capsys: pytest.CaptureFixture[str]) -> None:
    main(["healthcheck", "--json"])
    payload = json.loads(capsys.readouterr().out)
    required = {
        "db_integrity",
        "migrations_current",
        "upstream_reachability",
        "secrets_present",
        "clock_sanity",
        "last_snapshot_age",
        "repo_state",
        "operational_state",
    }
    assert required <= set(payload["checks"])


def test_no_command_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == EXIT_USAGE


def test_unknown_command_is_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["frobnicate"])
    assert excinfo.value.code == EXIT_USAGE


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == EXIT_OK
    assert __version__ in capsys.readouterr().out
