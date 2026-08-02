"""Quota tracker — rate windows, credit budgets, per-upstream enforcement."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dexpaprika.quota import (
    QuotaError,
    QuotaExceededError,
    QuotaTracker,
)
from dexpaprika.storage.db import connect
from dexpaprika.storage.migrations import migrate

START = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "t.db")
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tracker(conn: sqlite3.Connection, clock: FakeClock) -> QuotaTracker:
    t = QuotaTracker(conn, now=clock)
    t.ensure_providers()
    return t


def _add_provider(
    conn: sqlite3.Connection,
    name: str,
    rate_limit: int,
    rate_period: str,
    credit_limit: int | None = None,
    costs: dict[str, int] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO providers (name, base_url, rate_limit, rate_period, has_credits,"
        " credit_limit) VALUES (?, 'https://example.com', ?, ?, ?, ?)",
        (name, rate_limit, rate_period, 1 if credit_limit else 0, credit_limit),
    )
    provider_id = conn.execute("SELECT id FROM providers WHERE name = ?", (name,)).fetchone()["id"]
    for pattern, credits in (costs or {}).items():
        conn.execute(
            "INSERT INTO provider_endpoint_costs (provider_id, endpoint_pattern, credits)"
            " VALUES (?, ?, ?)",
            (provider_id, pattern, credits),
        )


class TestSeed:
    def test_seed_is_idempotent_and_loads_known_providers(self, tracker: QuotaTracker) -> None:
        tracker.ensure_providers()  # second run must not duplicate
        names = {s["provider"] for s in tracker.summaries()}
        assert {"dexpaprika", "gmx", "coinstats", "etherscan", "zerion"} <= names

    def test_dexpaprika_seed_values(self, conn: sqlite3.Connection, tracker: QuotaTracker) -> None:
        row = conn.execute("SELECT * FROM providers WHERE name = 'dexpaprika'").fetchone()
        assert row["rate_limit"] == 30
        assert row["rate_period"] == "minute"
        assert row["credit_limit"] == 200_000

    def test_coinstats_defi_weight_via_pattern(self, tracker: QuotaTracker) -> None:
        assert tracker.endpoint_credits("coinstats", "wallet/defi") == 400
        assert tracker.endpoint_credits("coinstats", "wallet/defi?chain=base") == 400
        assert tracker.endpoint_credits("coinstats", "coins") == 2

    def test_unweighted_default_is_one(self, tracker: QuotaTracker) -> None:
        assert tracker.endpoint_credits("dexpaprika", "networks/base/pools/x") == 1


class TestRateWindow:
    def test_limit_enforced_then_freed_by_time(
        self, conn: sqlite3.Connection, clock: FakeClock
    ) -> None:
        _add_provider(conn, "tiny", rate_limit=3, rate_period="second")
        tracker = QuotaTracker(conn, now=clock)
        for _ in range(3):
            verdict = tracker.check("tiny", "x")
            assert verdict.allowed
            tracker.record("tiny", "x")
        denied = tracker.check("tiny", "x")
        assert not denied.allowed
        assert denied.reason == "rate-limit"
        assert 0 < denied.wait_seconds <= 1
        clock.advance(1.1)
        assert tracker.check("tiny", "x").allowed

    def test_minute_window(self, conn: sqlite3.Connection, clock: FakeClock) -> None:
        _add_provider(conn, "permin", rate_limit=2, rate_period="minute")
        tracker = QuotaTracker(conn, now=clock)
        tracker.record("permin", "x")
        clock.advance(30)
        tracker.record("permin", "x")
        assert not tracker.check("permin", "x").allowed
        clock.advance(31)  # first call leaves the 60s window
        assert tracker.check("permin", "x").allowed

    def test_enforced_per_upstream_across_instances(
        self, conn: sqlite3.Connection, clock: FakeClock, tmp_path: Path
    ) -> None:
        _add_provider(conn, "shared", rate_limit=1, rate_period="minute")
        first = QuotaTracker(conn, now=clock)
        second_conn = connect(tmp_path / "t.db")
        try:
            second = QuotaTracker(second_conn, now=clock)
            first.record("shared", "x")
            assert not second.check("shared", "x").allowed  # sees first's call
        finally:
            second_conn.close()


class TestCreditBudget:
    def test_budget_denies_before_exceeding(
        self, conn: sqlite3.Connection, clock: FakeClock
    ) -> None:
        _add_provider(
            conn,
            "budgeted",
            rate_limit=1000,
            rate_period="minute",
            credit_limit=10,
            costs={"*": 4},
        )
        tracker = QuotaTracker(conn, now=clock)
        assert tracker.check("budgeted", "e").allowed
        tracker.record("budgeted", "e")  # 4
        tracker.record("budgeted", "e")  # 8
        denied = tracker.check("budgeted", "e")  # would be 12 > 10
        assert not denied.allowed
        assert denied.reason == "credit-budget"

    def test_budget_resets_next_month(self, conn: sqlite3.Connection, clock: FakeClock) -> None:
        _add_provider(
            conn,
            "monthly",
            rate_limit=1000,
            rate_period="minute",
            credit_limit=4,
            costs={"*": 4},
        )
        tracker = QuotaTracker(conn, now=clock)
        tracker.record("monthly", "e")
        assert not tracker.check("monthly", "e").allowed
        clock.advance(31 * 24 * 3600)  # into September
        assert tracker.check("monthly", "e").allowed

    def test_no_credit_limit_means_rate_only(
        self, conn: sqlite3.Connection, clock: FakeClock
    ) -> None:
        _add_provider(conn, "nocap", rate_limit=100, rate_period="minute", costs={"*": 400})
        tracker = QuotaTracker(conn, now=clock)
        for _ in range(50):
            assert tracker.check("nocap", "e").allowed
            tracker.record("nocap", "e")


class TestRecordAndSummary:
    def test_record_logs_resolved_credits(
        self, conn: sqlite3.Connection, tracker: QuotaTracker
    ) -> None:
        tracker.record("coinstats", "wallet/defi", status=200, latency_ms=123, correlation_id="c1")
        row = conn.execute("SELECT * FROM api_call_log").fetchone()
        assert row["credits"] == 400
        assert row["status"] == 200
        assert row["latency_ms"] == 123
        assert row["correlation_id"] == "c1"
        assert row["ts"].startswith("2026-08-02T12:00:00")

    def test_summary_shape(self, tracker: QuotaTracker) -> None:
        tracker.record("dexpaprika", "networks")
        summary = tracker.summary("dexpaprika")
        assert summary["provider"] == "dexpaprika"
        assert summary["window_used"] == 1
        assert summary["window_limit"] == 30
        assert summary["month_credits"] == 1
        assert summary["credit_limit"] == 200_000
        assert 0 < float(summary["pct_used"]) < 1

    def test_unknown_provider_raises(self, tracker: QuotaTracker) -> None:
        with pytest.raises(QuotaError, match="unknown provider"):
            tracker.check("nope", "x")


class TestWaitForSlot:
    def test_sleeps_until_rate_slot(self, conn: sqlite3.Connection, clock: FakeClock) -> None:
        _add_provider(conn, "s", rate_limit=1, rate_period="second")
        tracker = QuotaTracker(conn, now=clock)
        tracker.record("s", "x")
        slept: list[float] = []

        def sleeper(seconds: float) -> None:
            slept.append(seconds)
            clock.advance(seconds)

        tracker.wait_for_slot("s", "x", sleeper=sleeper)
        assert slept  # actually waited
        assert tracker.check("s", "x").allowed

    def test_credit_block_raises_instead_of_sleeping(
        self, conn: sqlite3.Connection, clock: FakeClock
    ) -> None:
        _add_provider(
            conn, "c", rate_limit=10, rate_period="second", credit_limit=1, costs={"*": 1}
        )
        tracker = QuotaTracker(conn, now=clock)
        tracker.record("c", "x")
        with pytest.raises(QuotaExceededError, match="credit"):
            tracker.wait_for_slot("c", "x", sleeper=lambda _s: None)


@given(gaps=st.lists(st.floats(min_value=0, max_value=3), min_size=1, max_size=40))
def test_property_guarded_calls_never_exceed_window_limit(
    tmp_path_factory: pytest.TempPathFactory, gaps: list[float]
) -> None:
    """For ANY call pattern, guarding with check() keeps every 1s window <= limit."""
    conn = connect(tmp_path_factory.mktemp("prop") / "t.db")
    try:
        migrate(conn)
        _add_provider(conn, "prop", rate_limit=3, rate_period="second")
        clock = FakeClock()
        tracker = QuotaTracker(conn, now=clock)
        recorded: list[datetime] = []
        for gap in gaps:
            clock.advance(gap)
            if tracker.check("prop", "x").allowed:
                tracker.record("prop", "x")
                recorded.append(clock.now)
        for i, ts in enumerate(recorded):
            window = [t for t in recorded[: i + 1] if (ts - t).total_seconds() < 1]
            assert len(window) <= 3
    finally:
        conn.close()
