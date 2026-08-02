"""S9 execution engine — every safeguard BLOCKS (standards §4), sidecar mocked."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from dexpaprika.config import Settings
from dexpaprika.execute.approval import ApprovalDecision
from dexpaprika.execute.engine import execute_instruction
from dexpaprika.execute.instruction import OrderInstruction
from dexpaprika.execute.safety import (
    ARMED_FILE,
    KILL_SWITCH_FILE,
    arm,
    check_armed,
    check_kill_switch,
    check_limits,
    trip_kill_switch,
)
from dexpaprika.storage.db import connect
from dexpaprika.storage.migrations import migrate

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
SL_ORDER_KEY = "0xc7c11d5c6267283c0605352adb0daefa0593f5c7707a534d71646ce8ea2ce642"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEXPAPRIKA_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "t.db")
    migrate(connection)
    yield connection
    connection.close()


def settings() -> Settings:
    return Settings.load()


def sl_instruction(price: str = "1926") -> OrderInstruction:
    return OrderInstruction(
        action="set-sl-trigger", order_key=SL_ORDER_KEY, trigger_price=Decimal(price)
    )


class FakeSidecar:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.submit_response: dict[str, Any] = {"ok": True, "request_id": "req-1"}

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(payload)
        if payload["mode"] == "prepare":
            return {"ok": True, "plan": {"fees_usd": "1.20", "trigger_1e30": "1926" + "0" * 30}}
        if payload["mode"] == "submit":
            return dict(self.submit_response)
        return {"ok": True, "orders": [{"key": SL_ORDER_KEY, "triggerPrice": "1926000000000000"}]}

    def modes(self) -> list[str]:
        return [c["mode"] for c in self.calls]


def approve_all(_instruction_id: str, _message: str) -> ApprovalDecision:
    return ApprovalDecision(approved=True, reason="approved by test")


def reject_all(_instruction_id: str, _message: str) -> ApprovalDecision:
    return ApprovalDecision(approved=False, reason="rejected by test")


def run(
    conn: sqlite3.Connection,
    *,
    arm_flag: bool = False,
    sidecar: FakeSidecar | None = None,
    approval: Any = approve_all,
    instruction: OrderInstruction | None = None,
    now: datetime = NOW,
    delta_usd: Decimal = Decimal(0),
    new_position_usd: Decimal = Decimal("13000"),
) -> tuple[Any, FakeSidecar]:
    side = sidecar or FakeSidecar()
    result = execute_instruction(
        conn,
        instruction or sl_instruction(),
        settings=settings(),
        sidecar=side,
        approval=approval,
        arm_flag=arm_flag,
        now=now,
        delta_usd=delta_usd,
        new_position_usd=new_position_usd,
    )
    return result, side


def audit_phases(conn: sqlite3.Connection) -> list[str]:
    return [r["phase"] for r in conn.execute("SELECT phase FROM audit_log ORDER BY id")]


class TestDryRunDefault:
    def test_dry_run_never_submits(self, conn: sqlite3.Connection) -> None:
        result, side = run(conn, arm_flag=False)
        assert result.status == "dry-run"
        assert result.plan is not None
        assert side.modes() == ["prepare"]  # a submit call in dry-run is impossible
        assert audit_phases(conn) == ["intent", "simulation"]

    def test_arm_flag_without_armed_file_blocks(self, conn: sqlite3.Connection) -> None:
        result, side = run(conn, arm_flag=True)  # no ARMED file created
        assert result.status == "blocked"
        assert "arm" in str(result.detail).lower()
        assert "submit" not in side.modes()
        assert "blocked" in audit_phases(conn)


class TestArmedState:
    def test_arm_creates_file_and_expires(self, tmp_path: Path) -> None:
        cfg = settings()
        armed_path = arm(cfg, ttl_minutes=30, now=NOW)
        assert armed_path.name == ARMED_FILE
        assert check_armed(cfg, arm_flag=True, now=NOW).allowed
        late = NOW + timedelta(minutes=31)
        assert not check_armed(cfg, arm_flag=True, now=late).allowed

    def test_no_arm_flag_never_armed(self) -> None:
        cfg = settings()
        arm(cfg, ttl_minutes=30, now=NOW)
        assert not check_armed(cfg, arm_flag=False, now=NOW).allowed

    def test_armed_e2e_submits_and_confirms(self, conn: sqlite3.Connection) -> None:
        arm(settings(), ttl_minutes=30, now=NOW)
        result, side = run(conn, arm_flag=True)
        assert result.status == "confirmed"
        assert side.modes() == ["prepare", "submit", "read"]  # post-condition verified
        phases = audit_phases(conn)
        assert phases == ["intent", "simulation", "submission", "confirmation"]


class TestKillSwitch:
    def test_kill_switch_blocks_everything(self, conn: sqlite3.Connection) -> None:
        cfg = settings()
        arm(cfg, ttl_minutes=30, now=NOW)
        trip_kill_switch(cfg, conn, "test trip", now=NOW)
        result, side = run(conn, arm_flag=True)
        assert result.status == "blocked"
        assert "kill" in str(result.detail).lower()
        assert side.calls == []  # gate is BEFORE any client/sidecar contact

    def test_arm_refuses_while_tripped(self, conn: sqlite3.Connection) -> None:
        cfg = settings()
        trip_kill_switch(cfg, conn, "test trip", now=NOW)
        with pytest.raises(RuntimeError, match="kill"):
            arm(cfg, ttl_minutes=30, now=NOW)

    def test_no_code_path_removes_the_file(self, conn: sqlite3.Connection) -> None:
        cfg = settings()
        trip_kill_switch(cfg, conn, "test trip", now=NOW)
        kill_file = cfg.data_dir / KILL_SWITCH_FILE
        assert kill_file.exists()
        import dexpaprika.execute.safety as safety_module

        assert not hasattr(safety_module, "clear_kill_switch")
        assert not hasattr(safety_module, "reset_kill_switch")

    def test_trip_is_audited(self, conn: sqlite3.Connection) -> None:
        trip_kill_switch(settings(), conn, "post-condition mismatch", now=NOW)
        row = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        assert row["phase"] == "blocked"
        assert "mismatch" in row["payload_json"]

    def test_three_consecutive_failures_trip(self, conn: sqlite3.Connection) -> None:
        cfg = settings()
        arm(cfg, ttl_minutes=120, now=NOW)
        side = FakeSidecar()
        side.submit_response = {"ok": False, "error": "relay_failed"}
        for i in range(3):
            moment = NOW + timedelta(minutes=2 * i)
            run(conn, arm_flag=True, sidecar=side, now=moment)
        assert check_kill_switch(cfg).allowed is False


class TestHardLimits:
    def test_position_cap_blocks(self, conn: sqlite3.Connection) -> None:
        gate = check_limits(
            conn,
            settings=settings(),
            now=NOW,
            market="ETH/USD",
            new_position_usd=Decimal("20001"),
            delta_usd=Decimal("100"),
        )
        assert not gate.allowed
        assert "position" in str(gate.reason)

    def test_delta_cap_blocks(self, conn: sqlite3.Connection) -> None:
        gate = check_limits(
            conn,
            settings=settings(),
            now=NOW,
            market="ETH/USD",
            new_position_usd=Decimal("13000"),
            delta_usd=Decimal("5001"),
        )
        assert not gate.allowed

    def test_wrong_market_blocks(self, conn: sqlite3.Connection) -> None:
        gate = check_limits(
            conn,
            settings=settings(),
            now=NOW,
            market="BTC/USD",
            new_position_usd=Decimal("1"),
            delta_usd=Decimal("1"),
        )
        assert not gate.allowed
        assert "market" in str(gate.reason)

    def _submission_row(self, conn: sqlite3.Connection, ts: datetime) -> None:
        conn.execute(
            "INSERT INTO audit_log (ts, actor, action, phase, payload_json)"
            " VALUES (?, 'executor', 'set-sl-trigger', 'submission', '{}')",
            (ts.isoformat(),),
        )

    def test_daily_adjustment_cap_blocks(self, conn: sqlite3.Connection) -> None:
        for i in range(4):
            self._submission_row(conn, NOW - timedelta(hours=i + 1))
        gate = check_limits(
            conn,
            settings=settings(),
            now=NOW,
            market="ETH/USD",
            new_position_usd=Decimal("1"),
            delta_usd=Decimal("1"),
        )
        assert not gate.allowed
        assert "daily" in str(gate.reason)

    def test_submission_rate_limit_blocks(self, conn: sqlite3.Connection) -> None:
        self._submission_row(conn, NOW - timedelta(seconds=30))
        gate = check_limits(
            conn,
            settings=settings(),
            now=NOW,
            market="ETH/USD",
            new_position_usd=Decimal("1"),
            delta_usd=Decimal("1"),
        )
        assert not gate.allowed
        assert "rate" in str(gate.reason)

    def test_within_all_limits_allowed(self, conn: sqlite3.Connection) -> None:
        gate = check_limits(
            conn,
            settings=settings(),
            now=NOW,
            market="ETH/USD",
            new_position_usd=Decimal("13000"),
            delta_usd=Decimal("4000"),
        )
        assert gate.allowed

    def test_limit_breach_blocks_engine_before_approval(self, conn: sqlite3.Connection) -> None:
        arm(settings(), ttl_minutes=30, now=NOW)
        approvals: list[str] = []

        def spy_approval(instruction_id: str, _msg: str) -> ApprovalDecision:
            approvals.append(instruction_id)
            return ApprovalDecision(approved=True, reason="x")

        result, side = run(conn, arm_flag=True, approval=spy_approval, delta_usd=Decimal("9999999"))
        assert result.status == "blocked"
        assert approvals == []
        assert "submit" not in side.modes()


class TestApproval:
    def test_rejection_blocks_submission(self, conn: sqlite3.Connection) -> None:
        arm(settings(), ttl_minutes=30, now=NOW)
        result, side = run(conn, arm_flag=True, approval=reject_all)
        assert result.status == "rejected"
        assert "submit" not in side.modes()
        assert "rejected" in audit_phases(conn)

    def test_approval_message_is_substantive(self, conn: sqlite3.Connection) -> None:
        """A bare yes cannot fire: the message must restate the parameters."""
        arm(settings(), ttl_minutes=30, now=NOW)
        seen: list[str] = []

        def capture(instruction_id: str, message: str) -> ApprovalDecision:
            seen.append(message)
            return ApprovalDecision(approved=True, reason="ok")

        run(conn, arm_flag=True, approval=capture)
        [message] = seen
        assert "set-sl-trigger" in message
        assert "1926" in message
        assert SL_ORDER_KEY[:10] in message


class TestIdempotency:
    def test_same_decision_same_hour_same_key(self) -> None:
        a = sl_instruction().idempotency_key(NOW)
        b = sl_instruction().idempotency_key(NOW + timedelta(minutes=20))
        c = sl_instruction().idempotency_key(NOW + timedelta(hours=2))
        assert a == b
        assert a != c
        assert sl_instruction("1930").idempotency_key(NOW) != a

    def test_confirmed_decision_replays_without_sidecar(self, conn: sqlite3.Connection) -> None:
        arm(settings(), ttl_minutes=60, now=NOW)
        first, side_a = run(conn, arm_flag=True)
        assert first.status == "confirmed"
        later = NOW + timedelta(minutes=5)
        second, side_b = run(conn, arm_flag=True, now=later)
        assert second.status == "replayed"
        assert side_b.calls == []  # stored response replayed verbatim
        assert first.response == second.response

    def test_crash_after_submission_resubmits_same_key_only(self, conn: sqlite3.Connection) -> None:
        """Submission row without confirmation (crash) — retry reuses the SAME
        idempotency key so the venue-side dedupe makes double-fire impossible."""
        arm(settings(), ttl_minutes=60, now=NOW)
        key = sl_instruction().idempotency_key(NOW)
        conn.execute(
            "INSERT INTO audit_log (ts, actor, action, phase, idempotency_key, payload_json)"
            " VALUES (?, 'executor', 'set-sl-trigger', 'submission', ?, '{}')",
            (NOW.isoformat(), key),
        )
        result, side = run(conn, arm_flag=True, now=NOW + timedelta(minutes=1))
        assert result.status == "confirmed"
        submits = [c for c in side.calls if c["mode"] == "submit"]
        assert len(submits) == 1
        assert submits[0]["idempotency_key"] == key


class TestPostConditions:
    def test_mismatch_trips_kill_switch(self, conn: sqlite3.Connection) -> None:
        cfg = settings()
        arm(cfg, ttl_minutes=30, now=NOW)

        class BadWorld(FakeSidecar):
            def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
                if payload["mode"] == "read":
                    self.calls.append(payload)
                    return {"ok": True, "orders": [{"key": "0xother", "triggerPrice": "1"}]}
                return super().__call__(payload)

        result, _side = run(conn, arm_flag=True, sidecar=BadWorld())
        assert result.status == "failed"
        assert not check_kill_switch(cfg).allowed  # auto-trip


class TestAuditCompleteness:
    def test_no_submission_without_prior_intent(self, conn: sqlite3.Connection) -> None:
        """Property over the pipeline: every submission row has an earlier
        intent row with the same idempotency key."""
        arm(settings(), ttl_minutes=600, now=NOW)
        for i, price in enumerate(["1926", "1930", "1921"]):
            run(
                conn,
                arm_flag=(i % 2 == 0),
                instruction=sl_instruction(price),
                now=NOW + timedelta(hours=i, minutes=3 * i),
            )
        rows = conn.execute(
            "SELECT id, phase, idempotency_key FROM audit_log ORDER BY id"
        ).fetchall()
        for row in rows:
            if row["phase"] == "submission":
                prior = [
                    r
                    for r in rows
                    if r["id"] < row["id"]
                    and r["phase"] == "intent"
                    and r["idempotency_key"] == row["idempotency_key"]
                ]
                assert prior, "submission without prior intent"
