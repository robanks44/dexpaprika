"""Alert rules engine — each rule fires on violation, stays silent when healthy."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dexpaprika.alerts.rules import (
    Alert,
    apply_cooldown,
    evaluate,
    mark_delivery,
    record_alert,
)
from dexpaprika.config import Settings
from dexpaprika.storage.db import connect
from dexpaprika.storage.migrations import migrate

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)

# Live S5/S4 fixture states (probe/verifier-recorded).
LP_STATE = {
    "tick_lower": -202000,
    "tick_upper": -200000,
    "liquidity": 3987414535131380,
    "price_usd": "1845.72",
}
PERP_STATE = {
    "size_tokens": "7.038573460810147061",
    "entry_price": "1869.094972567349999993975015634280",
    "stop_loss_triggers": ["1925"],
    "collateral_usd": "6579.725157",
}
HEALTH_OK = {"db_integrity": "ok", "migrations_current": "ok", "secrets_present": "ok"}


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "t.db")
    migrate(connection)
    yield connection
    connection.close()


def settings() -> Settings:
    return Settings.load()


def _insert_position(
    conn: sqlite3.Connection, kind: str, state: Mapping[str, object], *, ts: str
) -> None:
    group = "lp_hedge"
    cur = conn.execute(
        "INSERT INTO positions (wallet_ref, venue, chain, kind, external_id, group_tag)"
        " VALUES ('w', 'v', 'base', ?, ?, ?)",
        (kind, f"{kind}-1", group),
    )
    conn.execute(
        "INSERT INTO position_events (position_id, ts, type, delta_json, state_json)"
        " VALUES (?, ?, 'observed', '{}', ?)",
        (cur.lastrowid, ts, json.dumps(state)),
    )


def _insert_snapshot(conn: sqlite3.Connection, ts: str) -> None:
    conn.execute(
        "INSERT INTO snapshots (ts, chain, block_number, kind) VALUES (?, 'base', 1, 'lp')",
        (ts,),
    )


def _fresh(
    conn: sqlite3.Connection, lp: Mapping[str, object], perp: Mapping[str, object] | None
) -> None:
    ts = NOW.isoformat()
    _insert_position(conn, "lp", lp, ts=ts)
    if perp is not None:
        _insert_position(conn, "perp", perp, ts=ts)
    _insert_snapshot(conn, ts)


def rules_of(alerts: list[Alert]) -> set[str]:
    return {a.rule for a in alerts}


class TestHedgeRules:
    def test_naked_lp_fires_urgent(self, conn: sqlite3.Connection) -> None:
        _fresh(conn, LP_STATE, None)
        alerts = evaluate(conn, settings=settings(), now=NOW, health=HEALTH_OK)
        [alert] = [a for a in alerts if a.rule == "naked-lp"]
        assert alert.severity == "urgent"
        assert "short" in alert.message.lower() or "naked" in alert.message.lower()

    def test_live_fixture_fires_rebalance_needed(self, conn: sqlite3.Connection) -> None:
        """7.04 short vs ~5.03 delta is beyond the 7.5% band."""
        _fresh(conn, LP_STATE, PERP_STATE)
        alerts = evaluate(conn, settings=settings(), now=NOW, health=HEALTH_OK)
        [alert] = [a for a in alerts if a.rule == "rebalance-needed"]
        assert alert.severity == "high"
        assert "coverage_ratio_eth" in alert.firing_inputs

    def test_matched_short_is_silent(self, conn: sqlite3.Connection) -> None:
        """A delta-matched short fires no hedge rule (SL rules excluded)."""
        matched = dict(PERP_STATE, size_tokens="5.03", stop_loss_triggers=[])
        _fresh(conn, LP_STATE, matched)
        alerts = evaluate(conn, settings=settings(), now=NOW, health=HEALTH_OK)
        assert rules_of(alerts).isdisjoint(
            {"naked-lp", "rebalance-needed", "price-near-sl", "near-band-edge"}
        )

    def test_price_near_sl_fires_urgent(self, conn: sqlite3.Connection) -> None:
        near_sl = dict(LP_STATE, price_usd="1900")  # 1.3% from 1925
        _fresh(conn, near_sl, dict(PERP_STATE, size_tokens="5.10"))
        alerts = evaluate(conn, settings=settings(), now=NOW, health=HEALTH_OK)
        [alert] = [a for a in alerts if a.rule == "price-near-sl"]
        assert alert.severity == "urgent"

    def test_near_band_edge_fires_high(self, conn: sqlite3.Connection) -> None:
        near_floor = dict(LP_STATE, price_usd="1700")  # 0.63% above 1689.24 floor
        _fresh(conn, near_floor, PERP_STATE)
        alerts = evaluate(conn, settings=settings(), now=NOW, health=HEALTH_OK)
        [alert] = [a for a in alerts if a.rule == "near-band-edge"]
        assert alert.severity == "high"

    def test_no_lp_state_runs_non_hedge_rules_only(self, conn: sqlite3.Connection) -> None:
        """Empty DB: no hedge rules, but staleness fires — that IS the alert."""
        alerts = evaluate(conn, settings=settings(), now=NOW, health=HEALTH_OK)
        assert "snapshot-stale" in rules_of(alerts)
        assert rules_of(alerts).isdisjoint({"naked-lp", "rebalance-needed"})


class TestStaleness:
    def test_fresh_snapshot_silent(self, conn: sqlite3.Connection) -> None:
        _insert_snapshot(conn, (NOW - timedelta(minutes=30)).isoformat())
        alerts = evaluate(conn, settings=settings(), now=NOW, health=HEALTH_OK)
        assert "snapshot-stale" not in rules_of(alerts)

    def test_stale_snapshot_fires(self, conn: sqlite3.Connection) -> None:
        _insert_snapshot(conn, (NOW - timedelta(minutes=120)).isoformat())
        alerts = evaluate(conn, settings=settings(), now=NOW, health=HEALTH_OK)
        [alert] = [a for a in alerts if a.rule == "snapshot-stale"]
        assert alert.severity == "high"
        assert alert.firing_inputs["age_minutes"] >= 119

    def test_no_snapshots_at_all_fires(self, conn: sqlite3.Connection) -> None:
        alerts = evaluate(conn, settings=settings(), now=NOW, health=HEALTH_OK)
        assert "snapshot-stale" in rules_of(alerts)


class TestQuota:
    def _seed_credit_provider(self, conn: sqlite3.Connection, used: int, limit: int) -> None:
        cur = conn.execute(
            "INSERT INTO providers (name, base_url, rate_limit, rate_period,"
            " has_credits, credit_limit) VALUES ('capped', 'https://x', 1000, 'minute', 1, ?)",
            (limit,),
        )
        conn.execute(
            "INSERT INTO api_call_log (ts, provider_id, endpoint, credits) VALUES (?, ?, 'e', ?)",
            (NOW.isoformat(), cur.lastrowid, used),
        )

    def test_credit_budget_over_threshold_fires(self, conn: sqlite3.Connection) -> None:
        _insert_snapshot(conn, NOW.isoformat())
        self._seed_credit_provider(conn, used=850, limit=1000)  # 85% > 80% default
        alerts = evaluate(conn, settings=settings(), now=NOW, health=HEALTH_OK)
        [alert] = [a for a in alerts if a.rule == "quota-critical"]
        assert alert.firing_inputs["provider"] == "capped"

    def test_credit_budget_under_threshold_silent(self, conn: sqlite3.Connection) -> None:
        _insert_snapshot(conn, NOW.isoformat())
        self._seed_credit_provider(conn, used=100, limit=1000)
        alerts = evaluate(conn, settings=settings(), now=NOW, health=HEALTH_OK)
        assert "quota-critical" not in rules_of(alerts)

    def test_rate_window_never_alerts(self, conn: sqlite3.Connection) -> None:
        """Per-minute windows fill transiently by design — no credit_limit, no alert."""
        _insert_snapshot(conn, NOW.isoformat())
        cur = conn.execute(
            "INSERT INTO providers (name, base_url, rate_limit, rate_period, has_credits)"
            " VALUES ('ratey', 'https://x', 10, 'minute', 0)"
        )
        for _ in range(10):  # window 100% used
            conn.execute(
                "INSERT INTO api_call_log (ts, provider_id, endpoint, credits)"
                " VALUES (?, ?, 'e', 1)",
                (NOW.isoformat(), cur.lastrowid),
            )
        alerts = evaluate(conn, settings=settings(), now=NOW, health=HEALTH_OK)
        assert "quota-critical" not in rules_of(alerts)


class TestHealthRule:
    def test_failed_check_fires(self, conn: sqlite3.Connection) -> None:
        _insert_snapshot(conn, NOW.isoformat())
        health = dict(HEALTH_OK, db_integrity="fail: integrity_check reports 'bad'")
        alerts = evaluate(conn, settings=settings(), now=NOW, health=health)
        [alert] = [a for a in alerts if a.rule == "healthcheck-degraded"]
        assert alert.firing_inputs["failed"] == {
            "db_integrity": "fail: integrity_check reports 'bad'"
        }

    def test_all_ok_silent(self, conn: sqlite3.Connection) -> None:
        _insert_snapshot(conn, NOW.isoformat())
        alerts = evaluate(conn, settings=settings(), now=NOW, health=HEALTH_OK)
        assert "healthcheck-degraded" not in rules_of(alerts)


class TestCooldownAndLog:
    def _alert(self) -> Alert:
        return Alert(
            rule="snapshot-stale",
            severity="high",
            title="t",
            message="m",
            firing_inputs={"age_minutes": 120},
        )

    def test_recent_firing_suppressed(self, conn: sqlite3.Connection) -> None:
        alert_id = record_alert(conn, self._alert(), now=NOW)
        assert alert_id > 0
        fire, suppressed = apply_cooldown(
            conn, [self._alert()], settings=settings(), now=NOW + timedelta(minutes=30)
        )
        assert fire == []
        assert [a.rule for a in suppressed] == ["snapshot-stale"]

    def test_expired_cooldown_fires_again(self, conn: sqlite3.Connection) -> None:
        record_alert(conn, self._alert(), now=NOW)
        fire, suppressed = apply_cooldown(
            conn, [self._alert()], settings=settings(), now=NOW + timedelta(minutes=61)
        )
        assert [a.rule for a in fire] == ["snapshot-stale"]
        assert suppressed == []

    def test_suppression_counts_undelivered_rows(self, conn: sqlite3.Connection) -> None:
        """A recorded-but-undelivered firing still cools down (no retry spam)."""
        record_alert(conn, self._alert(), now=NOW)  # delivered stays 0
        fire, _ = apply_cooldown(
            conn, [self._alert()], settings=settings(), now=NOW + timedelta(minutes=5)
        )
        assert fire == []

    def test_record_then_mark_delivery(self, conn: sqlite3.Connection) -> None:
        alert_id = record_alert(conn, self._alert(), now=NOW)
        row = conn.execute("SELECT * FROM alerts_log WHERE id=?", (alert_id,)).fetchone()
        assert row["delivered"] == 0
        assert json.loads(row["payload_json"])["age_minutes"] == 120
        mark_delivery(conn, alert_id, delivered=True, ntfy_status="200")
        row = conn.execute("SELECT * FROM alerts_log WHERE id=?", (alert_id,)).fetchone()
        assert row["delivered"] == 1
        assert row["ntfy_status"] == "200"
