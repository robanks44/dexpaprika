"""Gate-suite tests for the S0 CLI stub.

Offline by construction (pytest-socket disables the network for the whole
suite via addopts). These tests pin the CLI contract the agent relies on:
JSON output, exit codes, honest degraded healthcheck.
"""

from __future__ import annotations

import json

import pytest

from dexpaprika import __version__
from dexpaprika.cli import (
    EXIT_DEGRADED,
    EXIT_OK,
    EXIT_USAGE,
    main,
)


def test_status_json_contract(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["status", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_OK
    assert payload["app"] == "dexpaprika"
    assert payload["version"] == __version__
    assert payload["phase"] == "scaffold"
    assert payload["sections_complete"] == ["s0"]


def test_status_human_output(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["status"])
    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "dexpaprika" in out
    # Human mode must not be JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_healthcheck_is_degraded_until_checks_exist(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["healthcheck", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_DEGRADED
    assert payload["healthy"] is False
    assert payload["degraded"] is True
    # Exit 0 only if ALL pass (ENGINEERING_STANDARDS §2) — nothing passes yet.
    assert set(payload["checks"].values()) == {"not-implemented"}


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
