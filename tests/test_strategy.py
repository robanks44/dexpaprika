"""S14 — delta-band rebalance strategy (offline). Extra scrutiny on auto-execute safety."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from dexpaprika.config import Settings
from dexpaprika.execute.safety import arm, trip_kill_switch
from dexpaprika.storage.db import connect
from dexpaprika.storage.migrations import migrate
from dexpaprika.strategy import evaluate, run

T0 = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)

LP_STATE: dict[str, Any] = {
    "tick_lower": -202000,
    "tick_upper": -200000,
    "liquidity": 3987414535131380,
    "price_usd": "1854.05",
    "in_range": True,
}


def _perp(size_eth: str) -> dict[str, Any]:
    return {
        "size_tokens": size_eth,
        "entry_price": "1900.0",
        "mark_price": "1854.05",
        "liquidation_price": "2100.0",
        "collateral_usd": "1000.0",
        "leverage": "1.85",
        "pnl": "46.0",
        "pending_funding_fees_usd": "1.0",
        "stop_loss_triggers": ["2050.0"],
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEXPAPRIKA_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")
    monkeypatch.delenv("DEXPAPRIKA_SECRET_NTFY_TOPIC", raising=False)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "s.db")
    migrate(c)
    from dexpaprika.quota import QuotaTracker

    QuotaTracker(c).ensure_providers()
    return c


def _settings() -> Settings:
    return Settings.load()


def _seed(conn: sqlite3.Connection, *, short_eth: str = "1.0", ts: datetime = T0) -> None:
    for kind, venue, chain, state in [
        ("lp", "aerodrome", "base", LP_STATE),
        ("perp", "gmx", "arbitrum", _perp(short_eth)),
    ]:
        conn.execute(
            "INSERT INTO positions (wallet_ref, venue, chain, kind, external_id, group_tag,"
            " opened_at) VALUES ('0xC155', ?, ?, ?, ?, 'lp_hedge', ?)",
            (venue, chain, kind, f"{kind}:1", ts.isoformat()),
        )
        pid = conn.execute("SELECT id FROM positions WHERE kind=?", (kind,)).fetchone()["id"]
        conn.execute(
            "INSERT INTO position_events (position_id, ts, type, delta_json, state_json)"
            " VALUES (?, ?, 'observed', '{}', ?)",
            (pid, ts.isoformat(), json.dumps(state)),
        )
    conn.execute(
        "INSERT INTO snapshots (ts, chain, block_number, kind) VALUES (?, 'base', 1, 'lp'),"
        " (?, 'arbitrum', NULL, 'hedge')",
        (ts.isoformat(), ts.isoformat()),
    )


def _executed_row(conn: sqlite3.Connection, ts: datetime) -> None:
    conn.execute(
        "INSERT INTO rebalance_log (ts, decision, executed) VALUES (?, 'execute', 1)",
        (ts.isoformat(),),
    )


NOW = T0 + timedelta(minutes=5)  # fresh (< 90m stale threshold)


# --------------------------- 1. evaluate execute / hold ---------------------------


def test_evaluate_execute_when_band_breached_and_gates_pass(conn: sqlite3.Connection) -> None:
    _seed(conn, short_eth="1.0")  # under-hedged vs ~4.8 ETH delta → band breached
    d = evaluate(conn, _settings(), now=NOW)
    assert d.decision == "execute"
    assert d.blocked_by == []
    assert d.gates.band_breached and d.gates.fresh and d.gates.cost_ok
    assert d.gates.auto_enabled is False  # dormant by default
    assert "SHADOW" in d.reason  # warranted but not enabled
    assert d.target_eth is not None and d.current_eth == 1


def test_target_clamped_to_per_run_cap(conn: sqlite3.Connection) -> None:
    # Big gap (short 1.0 vs ~4.8 delta): the resize target is clamped so one step never
    # exceeds max_delta_per_run_usd — it converges over cycles instead of blocking forever.
    _seed(conn, short_eth="1.0")
    d = evaluate(conn, _settings(), now=NOW)
    assert d.decision == "execute"
    assert d.target_eth is not None and d.current_eth is not None
    assert d.target_eth < Decimal("4.8")  # clamped below the full delta-match target
    assert d.est_move_usd is not None and d.est_move_usd <= _settings().max_delta_per_run_usd


def test_evaluate_hold_when_within_band(conn: sqlite3.Connection) -> None:
    _seed(conn, short_eth="4.82")  # ~matched to LP delta → within band
    d = evaluate(conn, _settings(), now=NOW)
    assert d.decision == "hold"
    assert d.gates.band_breached is False


def test_evaluate_blocked_without_state(conn: sqlite3.Connection) -> None:
    d = evaluate(conn, _settings(), now=NOW)
    assert d.decision == "blocked" and "no-state" in d.blocked_by


def test_evaluate_blocks_on_non_positive_price(conn: sqlite3.Connection) -> None:
    # Defensive: bad data (price 0) must block, not crash the strategy job (fresh-agent S14).
    lp = dict(LP_STATE)
    lp["price_usd"] = "0"
    conn.execute(
        "INSERT INTO positions (wallet_ref, venue, chain, kind, external_id, group_tag, opened_at)"
        " VALUES ('0xC155', 'aerodrome', 'base', 'lp', 'lp:1', 'lp_hedge', ?)",
        (NOW.isoformat(),),
    )
    pid = conn.execute("SELECT id FROM positions WHERE kind='lp'").fetchone()["id"]
    conn.execute(
        "INSERT INTO position_events (position_id, ts, type, delta_json, state_json)"
        " VALUES (?, ?, 'observed', '{}', ?)",
        (pid, NOW.isoformat(), json.dumps(lp)),
    )
    conn.execute(
        "INSERT INTO snapshots (ts, chain, block_number, kind) VALUES (?, 'base', 1, 'lp')",
        (NOW.isoformat(),),
    )
    d = evaluate(conn, _settings(), now=NOW)
    assert d.decision == "blocked" and "bad-price" in d.blocked_by


# --------------------------- 2. each gate blocks independently ---------------------------


def test_gate_stale_state_blocks(conn: sqlite3.Connection) -> None:
    _seed(conn, short_eth="1.0")
    d = evaluate(conn, _settings(), now=T0 + timedelta(hours=3))  # > 90m stale
    assert d.decision == "blocked" and "stale-state" in d.blocked_by


def test_gate_cost_floor_blocks(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEXPAPRIKA_REBALANCE_MIN_NOTIONAL_USD", "1000000")
    _seed(conn, short_eth="1.0")
    d = evaluate(conn, _settings(), now=NOW)
    assert d.decision == "blocked" and "cost-floor" in d.blocked_by


def test_gate_min_interval_blocks(conn: sqlite3.Connection) -> None:
    _seed(conn, short_eth="1.0")
    _executed_row(conn, NOW - timedelta(minutes=10))  # last rebalance 10m ago (< 60m)
    d = evaluate(conn, _settings(), now=NOW)
    assert d.decision == "blocked" and "min-interval" in d.blocked_by


def test_gate_daily_limit_blocks(conn: sqlite3.Connection) -> None:
    _seed(conn, short_eth="1.0")
    for i in range(4):  # max_daily_adjustments default 4
        _executed_row(conn, T0 - timedelta(hours=6) + timedelta(minutes=i))
    d = evaluate(conn, _settings(), now=NOW)
    assert d.decision == "blocked" and "daily-limit" in d.blocked_by


def test_gate_max_position_blocks(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEXPAPRIKA_MAX_POSITION_USD", "100")  # target notional far exceeds
    _seed(conn, short_eth="1.0")
    d = evaluate(conn, _settings(), now=NOW)
    assert d.decision == "blocked" and "max-position" in d.blocked_by


# --------------------------- 3. run dry-run / shadow ---------------------------


class _FakeSidecar:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(payload)
        if payload["mode"] == "prepare":
            return {"ok": True, "plan": {"fees_usd": "1.20"}}
        if payload["mode"] == "submit":
            return {"ok": True, "request_id": "req-1"}
        return {"ok": True, "orders": []}

    def modes(self) -> list[str]:
        return [c["mode"] for c in self.calls]


def test_run_shadow_when_not_armed_executes_nothing(conn: sqlite3.Connection) -> None:
    _seed(conn, short_eth="1.0")
    side = _FakeSidecar()
    out = run(conn, _settings(), now=NOW, arm=False, sidecar=side)
    assert out.executed is False and out.shadow is True
    assert "submit" not in side.modes()  # NOTHING sent
    logged = conn.execute("SELECT decision, executed FROM rebalance_log").fetchone()
    assert logged["decision"] == "execute" and logged["executed"] == 0  # journaled, not traded


def test_run_shadow_when_arm_but_flag_disabled(conn: sqlite3.Connection) -> None:
    # arm=True but auto_rebalance_enabled defaults False → still shadow (defense in depth).
    _seed(conn, short_eth="1.0")
    arm(_settings(), now=NOW)
    side = _FakeSidecar()
    out = run(conn, _settings(), now=NOW, arm=True, sidecar=side)
    assert out.executed is False and out.shadow is True
    assert "submit" not in side.modes()


# --------------------------- 4. run auto-execute (opt-in ON) ---------------------------


def test_run_auto_executes_via_s9_pipeline(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEXPAPRIKA_AUTO_REBALANCE_ENABLED", "true")
    monkeypatch.setenv("DEXPAPRIKA_SECRET_NTFY_TOPIC", "uig-test")
    _seed(conn, short_eth="3.0")  # gap ~$3.4k < per-run cap → one clean resize
    arm(_settings(), now=NOW)
    side = _FakeSidecar()
    published: list[dict[str, Any]] = []

    def ntfy_factory(base: str) -> httpx.Client:
        def handler(req: httpx.Request) -> httpx.Response:
            published.append(json.loads(req.content))
            return httpx.Response(200, json={"id": "x", "time": 1, "event": "message"})

        return httpx.Client(transport=httpx.MockTransport(handler), base_url=base)

    out = run(conn, _settings(), now=NOW, arm=True, sidecar=side, client_factory=ntfy_factory)
    assert out.executed is True and out.shadow is False
    assert "submit" in side.modes()
    submit = next(c for c in side.calls if c["mode"] == "submit")
    assert submit["action"] == "resize-short" and submit["params"]["target_eth"] is not None
    row = conn.execute("SELECT executed, idempotency_key FROM rebalance_log").fetchone()
    assert row["executed"] == 1 and row["idempotency_key"]
    assert out.notified is True and published[0]["title"].startswith("dexpaprika")


# --------------------------- 5. S9 guards still block auto-execute ---------------------------


def test_kill_switch_blocks_even_with_auto_enabled(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEXPAPRIKA_AUTO_REBALANCE_ENABLED", "true")
    _seed(conn, short_eth="1.0")
    arm(_settings(), now=NOW)
    trip_kill_switch(_settings(), conn, "test trip", now=NOW)
    side = _FakeSidecar()
    out = run(conn, _settings(), now=NOW, arm=True, sidecar=side)
    assert out.executed is False  # executor blocked despite auto-enabled + armed
    assert "submit" not in side.modes()
    assert out.result is not None and out.result["status"] == "blocked"


# --------------------------- 6. CLI ---------------------------


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    from dexpaprika.cli import main

    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_cli_strategy_status_and_rebalance_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    from dexpaprika.cli import EXIT_OK
    from dexpaprika.storage.db import db_path

    _cli(capsys, "db", "migrate")
    c = connect(db_path(_settings()))
    try:
        _seed(c, short_eth="1.0", ts=datetime.now(UTC))  # CLI uses real now → seed fresh
    finally:
        c.close()
    code, out = _cli(capsys, "strategy", "status")
    assert code == EXIT_OK
    assert out["decision"] == "execute" and out["gates"]["auto_enabled"] is False
    # rebalance with --arm but flag disabled → shadow, trades nothing
    code, out = _cli(capsys, "strategy", "rebalance", "--arm")
    assert code == EXIT_OK
    assert out["executed"] is False and out["shadow"] is True
