"""CLI execute — separate privileged scope, dry-run default (S9)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dexpaprika.cli import EXIT_DEGRADED, EXIT_FAILURE, EXIT_OK, main

SL_ORDER_KEY = "0xc7c11d5c6267283c0605352adb0daefa0593f5c7707a534d71646ce8ea2ce642"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEXPAPRIKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")
    monkeypatch.setenv("DEXPAPRIKA_SECRET_NTFY_TOPIC", "test-topic")


@pytest.fixture
def sidecar_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_sidecar(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        if payload["mode"] == "prepare":
            return {"ok": True, "plan": {"fees_usd": "1.20"}}
        if payload["mode"] == "read":
            return {
                "ok": True,
                "orders": [{"key": SL_ORDER_KEY, "triggerPrice": "1926000000000000"}],
            }
        return {"ok": True, "request_id": "req-cli"}

    monkeypatch.setattr("dexpaprika.cli._sidecar_runner", lambda _settings: fake_sidecar)
    return calls


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_dry_run_is_the_default(
    capsys: pytest.CaptureFixture[str], sidecar_calls: list[dict[str, Any]]
) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(capsys, "execute", "set-sl-trigger", "--price", "1926")
    assert code == EXIT_OK
    assert out["status"] == "dry-run"
    assert out["plan"] is not None
    assert all(c["mode"] != "submit" for c in sidecar_calls)


def test_arm_flag_alone_is_blocked(
    capsys: pytest.CaptureFixture[str], sidecar_calls: list[dict[str, Any]]
) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(capsys, "execute", "set-sl-trigger", "--price", "1926", "--arm")
    assert code == EXIT_DEGRADED
    assert out["status"] == "blocked"
    assert all(c["mode"] != "submit" for c in sidecar_calls)


def test_execute_arm_then_status(capsys: pytest.CaptureFixture[str]) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(capsys, "execute", "arm", "--ttl-minutes", "15")
    assert code == EXIT_OK
    assert out["armed"] is True
    code, status = run_json(capsys, "execute", "status")
    assert code == EXIT_OK
    assert status["armed"] is True
    assert status["kill_switch"] is False
    limits = status["limits"]
    assert isinstance(limits, dict)
    assert limits["max_position_usd"] == "20000"
    assert limits["allowed_markets"] == ["ETH/USD"]


def test_kill_switch_file_blocks_arm(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    run_json(capsys, "db", "migrate")
    (tmp_path / "data" / "KILL-SWITCH").write_text("manual")
    code, out = run_json(capsys, "execute", "arm")
    assert code == EXIT_FAILURE
    assert "kill" in str(out["error"]).lower()


def test_execute_requires_db(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run_json(capsys, "execute", "set-sl-trigger", "--price", "1926")
    assert code == EXIT_FAILURE
    assert "migrate" in str(out["error"])
