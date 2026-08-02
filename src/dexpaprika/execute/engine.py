"""S9 execution pipeline — audit before action, gates before client.

Order of operations (every step audited, failures included):
intent → replay check → kill switch → armed → hard limits → prepare
(simulation) → [dry-run STOPS] → approval → submission (audited BEFORE
the sidecar is invoked) → submit → post-condition verify → confirmation.

The sidecar is injected: a callable taking one JSON-able payload dict and
returning a dict. Production wires the Node subprocess; tests wire fakes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from dexpaprika.config import Settings
from dexpaprika.execute.approval import ApprovalDecision
from dexpaprika.execute.instruction import OrderInstruction
from dexpaprika.execute.safety import (
    CONSECUTIVE_FAILURES_TO_TRIP,
    check_armed,
    check_kill_switch,
    check_limits,
    consecutive_submit_failures,
    trip_kill_switch,
)

Sidecar = Callable[[dict[str, Any]], dict[str, Any]]
Approval = Callable[[str, str], ApprovalDecision]


class ExecutionResult(BaseModel, frozen=True):
    status: str  # dry-run | blocked | rejected | replayed | confirmed | failed
    detail: str
    plan: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    idempotency_key: str


def _audit(
    conn: sqlite3.Connection,
    *,
    ts: datetime,
    action: str,
    phase: str,
    key: str | None,
    payload: dict[str, Any],
) -> int:
    cursor = conn.execute(
        "INSERT INTO audit_log (ts, actor, action, phase, idempotency_key, payload_json)"
        " VALUES (?, 'executor', ?, ?, ?, ?)",
        (ts.isoformat(), action, phase, key, json.dumps(payload, default=str)),
    )
    return int(cursor.lastrowid or 0)


def _stored_confirmation(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload_json FROM audit_log WHERE phase='confirmation'"
        " AND idempotency_key=? ORDER BY id DESC LIMIT 1",
        (key,),
    ).fetchone()
    if row is None:
        return None
    payload: dict[str, Any] = json.loads(row["payload_json"])
    response = payload.get("response")
    return response if isinstance(response, dict) else payload


def _postconditions_hold(instruction: OrderInstruction, read_response: dict[str, Any]) -> bool:
    orders = {o.get("key"): o for o in read_response.get("orders", [])}
    if instruction.action == "set-sl-trigger":
        order = orders.get(instruction.order_key)
        if order is None:
            return False
        # SDK/REST trigger scaling: 1e12 for ETH (VERIFIED_FINDINGS §2.1).
        expected = (instruction.trigger_price or Decimal(0)) * Decimal(10) ** 12
        return Decimal(str(order.get("triggerPrice", "0"))) == expected
    if instruction.action == "cancel-order":
        return instruction.order_key not in orders
    return True  # resize-short verified against position size by the caller's re-snapshot


def execute_instruction(
    conn: sqlite3.Connection,
    instruction: OrderInstruction,
    *,
    settings: Settings,
    sidecar: Sidecar,
    approval: Approval,
    arm_flag: bool,
    now: datetime,
    delta_usd: Decimal,
    new_position_usd: Decimal,
) -> ExecutionResult:
    key = instruction.idempotency_key(now)
    _audit(
        conn,
        ts=now,
        action=instruction.action,
        phase="intent",
        key=key,
        payload={"instruction": instruction.model_dump(), "arm_flag": arm_flag},
    )

    stored = _stored_confirmation(conn, key)
    if stored is not None:
        return ExecutionResult(
            status="replayed",
            detail="decision already executed this hour — stored response replayed verbatim",
            response=stored,
            idempotency_key=key,
        )

    def blocked(reason: str) -> ExecutionResult:
        _audit(
            conn,
            ts=now,
            action=instruction.action,
            phase="blocked",
            key=key,
            payload={"reason": reason},
        )
        return ExecutionResult(status="blocked", detail=reason, idempotency_key=key)

    kill = check_kill_switch(settings)
    if not kill.allowed:
        return blocked(kill.reason or "kill switch")

    armed = check_armed(settings, arm_flag=arm_flag, now=now)
    live = armed.allowed
    if arm_flag and not live:
        return blocked(armed.reason or "not armed")

    limits = check_limits(
        conn,
        settings=settings,
        now=now,
        market=instruction.market,
        new_position_usd=new_position_usd,
        delta_usd=delta_usd,
    )
    if not limits.allowed:
        return blocked(limits.reason or "hard limit")

    prepared = sidecar(
        {"mode": "prepare", "action": instruction.action, "params": instruction.model_dump()}
    )
    if not prepared.get("ok"):
        return blocked(f"prepare failed: {prepared.get('error', 'unknown')}")
    plan = prepared.get("plan", {})
    _audit(
        conn,
        ts=now,
        action=instruction.action,
        phase="simulation",
        key=key,
        payload={"plan": plan, "delta_usd": delta_usd, "new_position_usd": new_position_usd},
    )

    if not live:
        return ExecutionResult(
            status="dry-run",
            detail="dry-run (default): plan built and simulated; nothing sent."
            " Use `execute arm` + --arm to go live.",
            plan=plan,
            idempotency_key=key,
        )

    message = (
        f"{instruction.summary()}\n"
        f"plan: {json.dumps(plan, default=str)}\n"
        f"delta ${delta_usd}, resulting position ${new_position_usd}\n"
        f"instruction id: {key[:16]}"
    )
    decision = approval(key[:16], message)
    if not decision.approved:
        _audit(
            conn,
            ts=now,
            action=instruction.action,
            phase="rejected",
            key=key,
            payload={"reason": decision.reason},
        )
        return ExecutionResult(status="rejected", detail=decision.reason, idempotency_key=key)

    _audit(
        conn,
        ts=now,
        action=instruction.action,
        phase="submission",
        key=key,
        payload={"plan": plan, "approval": decision.reason},
    )
    response = sidecar(
        {
            "mode": "submit",
            "action": instruction.action,
            "params": instruction.model_dump(),
            "idempotency_key": key,
        }
    )
    if not response.get("ok"):
        _audit(
            conn,
            ts=now,
            action="submit-failed",
            phase="rejected",
            key=key,
            payload={"response": response},
        )
        if consecutive_submit_failures(conn) >= CONSECUTIVE_FAILURES_TO_TRIP:
            trip_kill_switch(
                settings,
                conn,
                f"{CONSECUTIVE_FAILURES_TO_TRIP} consecutive failed submissions",
                now=now,
            )
        return ExecutionResult(
            status="failed",
            detail=f"submission failed: {response.get('error', 'unknown')}",
            response=response,
            idempotency_key=key,
        )

    verify = sidecar({"mode": "read", "action": "read-orders", "params": {}})
    if not _postconditions_hold(instruction, verify):
        trip_kill_switch(
            settings,
            conn,
            f"post-condition mismatch after {instruction.action} ({key[:16]})",
            now=now,
        )
        return ExecutionResult(
            status="failed",
            detail="post-condition mismatch — kill switch tripped; verify manually",
            response=response,
            idempotency_key=key,
        )

    _audit(
        conn,
        ts=now,
        action=instruction.action,
        phase="confirmation",
        key=key,
        payload={"response": response, "verified": True},
    )
    return ExecutionResult(
        status="confirmed",
        detail="submitted, relayed, and post-conditions verified",
        response=response,
        idempotency_key=key,
    )
