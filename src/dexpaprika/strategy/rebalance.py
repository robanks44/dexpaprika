"""Delta-band rebalance strategy (S14).

North star: optimize for the greatest NET CAPITAL position — penalize fee churn,
unhedged-delta losses, AND catastrophic failure. So: (1) the decision rule is
benefit-vs-cost (v1 proxy: a notional cost-floor), (2) auto-execution is OPT-IN and
ships DORMANT (shadow → measure → tune → enable), and (3) every decision (shadow
included) is journaled for net-capital attribution.

Auto-execute does NOT bypass S9: ``run`` reuses ``execute.engine.execute_instruction``
with an auto-approving callback, so kill-switch, armed-state, hard limits, audit,
post-condition verify, and idempotency all still fire. S14 only supplies the "yes"
automatically — and only when its own gates pass AND ``auto_rebalance_enabled``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel

from dexpaprika.config import Settings

Sidecar = Callable[[dict[str, Any]], dict[str, Any]]
NtfyFactory = Callable[[str], httpx.Client]


class GateStates(BaseModel):
    band_breached: bool
    fresh: bool
    interval_ok: bool
    cost_ok: bool
    daily_ok: bool
    within_max_position: bool
    auto_enabled: bool


class RebalanceDecision(BaseModel):
    decision: str  # hold | execute | blocked
    current_eth: Decimal | None = None
    target_eth: Decimal | None = None
    deviation: Decimal | None = None
    band: Decimal
    price_usd: Decimal | None = None
    est_move_usd: Decimal | None = None
    reason: str
    blocked_by: list[str] = []
    gates: GateStates
    thesis: str


class RebalanceOutcome(BaseModel):
    decision: RebalanceDecision
    executed: bool
    shadow: bool
    result: dict[str, Any] | None = None
    notified: bool = False


# --------------------------- evaluation ---------------------------


def _today_executed(conn: sqlite3.Connection, now: datetime) -> list[str]:
    day = now.date().isoformat()
    rows = conn.execute(
        "SELECT ts FROM rebalance_log WHERE executed=1 AND substr(ts,1,10)=? ORDER BY id DESC",
        (day,),
    ).fetchall()
    return [r["ts"] for r in rows]


def _last_executed_ts(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT ts FROM rebalance_log WHERE executed=1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["ts"] if row else None


def _thesis(
    decision: str,
    current: Decimal | None,
    target: Decimal | None,
    deviation: Decimal | None,
    band: Decimal,
) -> str:
    if decision == "hold":
        return f"hold: delta within band (dev {deviation} ≤ {band})"
    move = "?" if (current is None or target is None) else f"{current}→{target} ETH"
    return (
        f"delta gap {deviation} > band {band}; resize short {move} to re-match LP exposure. "
        "Expected: net delta ~0, cut unhedged-delta risk; cost = gas+keeper+spread. "
        "Net-capital thesis: hedging benefit > rebalance cost (v1 proxy: notional floor)."
    )


def evaluate(conn: sqlite3.Connection, settings: Settings, *, now: datetime) -> RebalanceDecision:
    """Read-only rebalance decision from the latest recorded state + strategy gates."""
    from dexpaprika.hedge.engine import analyze
    from dexpaprika.hedge.state import latest_inputs
    from dexpaprika.watchdog.heartbeat import assess_health

    band = settings.hedge_rebalance_band
    inputs = latest_inputs(conn)
    if inputs is None:
        gates = GateStates(
            band_breached=False,
            fresh=False,
            interval_ok=False,
            cost_ok=False,
            daily_ok=False,
            within_max_position=False,
            auto_enabled=settings.auto_rebalance_enabled,
        )
        return RebalanceDecision(
            decision="blocked",
            band=band,
            reason="no recorded hedge state (run the recorder)",
            blocked_by=["no-state"],
            gates=gates,
            thesis="blocked: no recorded hedge state",
        )

    lp, short, price = inputs
    analysis = analyze(lp, short, price, settings=settings)
    current = analysis.short_size_eth
    full_target = analysis.delta_matched_target_eth
    max_delta = analysis.lp_delta_max_eth
    # Band-breach is judged on the FULL delta gap.
    deviation = (abs(current - full_target) / max_delta) if max_delta > 0 else Decimal(0)
    band_breached = analysis.rebalance_needed
    # Per-run step clamp: never resize more than max_delta_per_run_usd in one action, so a
    # large gap converges over several cycles instead of blocking on the hard limit forever
    # (net-capital: make progress; safety: never exceed the per-run cap). `target` is the
    # ACTUAL resize target this run.
    target = full_target
    cap_usd = settings.max_delta_per_run_usd
    if cap_usd > 0 and price > 0:
        # Keep a 1-cent margin so Decimal rounding never pushes est_move past the strict cap.
        max_step_eth = (cap_usd - Decimal("0.01")) / price
        gap = full_target - current
        if abs(gap) > max_step_eth:
            target = current + (max_step_eth if gap > 0 else -max_step_eth)
    est_move = abs(target - current) * price

    fresh = assess_health(conn, settings, now=now).ok
    last = _last_executed_ts(conn)
    interval_ok = True
    if last is not None:
        elapsed_min = (now - datetime.fromisoformat(last)).total_seconds() / 60
        interval_ok = elapsed_min >= settings.rebalance_min_interval_minutes
    cost_ok = est_move >= settings.rebalance_min_notional_usd
    daily_ok = (
        settings.max_daily_adjustments <= 0
        or len(_today_executed(conn, now)) < settings.max_daily_adjustments
    )
    within_max = settings.max_position_usd <= 0 or (target * price) <= settings.max_position_usd

    gates = GateStates(
        band_breached=band_breached,
        fresh=fresh,
        interval_ok=interval_ok,
        cost_ok=cost_ok,
        daily_ok=daily_ok,
        within_max_position=within_max,
        auto_enabled=settings.auto_rebalance_enabled,
    )
    safety_economic = {
        "stale-state": fresh,
        "min-interval": interval_ok,
        "cost-floor": cost_ok,
        "daily-limit": daily_ok,
        "max-position": within_max,
    }
    blocked_by: list[str] = []
    if not band_breached:
        decision, reason = "hold", f"delta within band (dev {deviation} ≤ {band})"
    elif all(safety_economic.values()):
        decision = "execute"
        reason = (
            f"rebalance warranted: dev {deviation} > band {band}; resize {current}→{target} ETH"
        )
        if not settings.auto_rebalance_enabled:
            reason += " — SHADOW (auto_rebalance_enabled=False)"
    else:
        decision = "blocked"
        blocked_by = [name for name, ok in safety_economic.items() if not ok]
        reason = f"rebalance warranted but blocked by: {', '.join(blocked_by)}"

    return RebalanceDecision(
        decision=decision,
        current_eth=current,
        target_eth=target,
        deviation=deviation,
        band=band,
        price_usd=price,
        est_move_usd=est_move,
        reason=reason,
        blocked_by=blocked_by,
        gates=gates,
        thesis=_thesis(decision, current, target, deviation, band),
    )


# --------------------------- journal + notify ---------------------------


def _newest_snapshot_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(id) AS m FROM snapshots").fetchone()
    return int(row["m"]) if row and row["m"] is not None else None


def _log_decision(conn: sqlite3.Connection, now: datetime, d: RebalanceDecision) -> int:
    cur = conn.execute(
        "INSERT INTO rebalance_log (ts, decision, current_eth, target_eth, deviation, band,"
        " price_usd, est_move_usd, executed, snapshot_id, gates_json, thesis)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
        (
            now.isoformat(),
            d.decision,
            _s(d.current_eth),
            _s(d.target_eth),
            _s(d.deviation),
            _s(d.band),
            _s(d.price_usd),
            _s(d.est_move_usd),
            _newest_snapshot_id(conn),
            json.dumps(d.gates.model_dump()),
            d.thesis,
        ),
    )
    return int(cur.lastrowid or 0)


def _s(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _notify(
    conn: sqlite3.Connection,
    settings: Settings,
    title: str,
    message: str,
    *,
    client_factory: NtfyFactory | None,
) -> bool:
    from dexpaprika.alerts.ntfy import NtfyClient
    from dexpaprika.secrets import resolve_provider

    topic = resolve_provider(settings).get("ntfy_topic")
    if topic is None:
        return False
    factory = client_factory or (
        lambda base: httpx.Client(
            base_url=base, timeout=30.0, headers={"User-Agent": "dexpaprika/1.0"}
        )
    )
    try:
        NtfyClient(
            conn, settings=settings, client=factory(settings.ntfy_server), topic=topic
        ).publish(title, message, priority="high", tags=["repeat"])
    except Exception:  # notification is best-effort; never block/raise on the trade path
        return False
    return True


# --------------------------- run (auto-execute via S9 pipeline) ---------------------------


def run(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    now: datetime,
    arm: bool,
    sidecar: Sidecar,
    client_factory: NtfyFactory | None = None,
) -> RebalanceOutcome:
    """Evaluate → journal → (only if warranted AND armed AND enabled) auto-execute via S9."""
    from dexpaprika.execute.approval import ApprovalDecision
    from dexpaprika.execute.engine import execute_instruction
    from dexpaprika.execute.instruction import OrderInstruction

    decision = evaluate(conn, settings, now=now)
    row_id = _log_decision(conn, now, decision)

    live = arm and settings.auto_rebalance_enabled
    if decision.decision != "execute" or not live:
        # hold / blocked / shadow (warranted but not enabled) — journal only, no trade.
        return RebalanceOutcome(
            decision=decision, executed=False, shadow=(decision.decision == "execute")
        )

    target = decision.target_eth or Decimal(0)
    current = decision.current_eth or Decimal(0)
    price = decision.price_usd or Decimal(0)
    instruction = OrderInstruction(action="resize-short", target_eth=target)

    def _auto_approve(_instruction_id: str, _message: str) -> ApprovalDecision:
        # Auto-execute posture: approve automatically. Every S9 guard already ran BEFORE
        # this callback (kill-switch, armed, hard limits); this only supplies the "yes".
        return ApprovalDecision(approved=True, reason="S14 auto-rebalance: strategy gates passed")

    result = execute_instruction(
        conn,
        instruction,
        settings=settings,
        sidecar=sidecar,
        approval=_auto_approve,
        arm_flag=True,
        now=now,
        delta_usd=abs(target - current) * price,
        new_position_usd=target * price,
    )
    executed = result.status == "confirmed"
    conn.execute(
        "UPDATE rebalance_log SET executed=?, idempotency_key=?, outcome_json=? WHERE id=?",
        (
            1 if executed else 0,
            result.idempotency_key,
            json.dumps(result.model_dump(mode="json")),
            row_id,
        ),
    )
    notified = _notify(
        conn,
        settings,
        f"dexpaprika — auto-rebalance {result.status}",
        f"resize short {current}→{target} ETH (dev {decision.deviation}); {result.detail}",
        client_factory=client_factory,
    )
    return RebalanceOutcome(
        decision=decision,
        executed=executed,
        shadow=False,
        result=result.model_dump(mode="json"),
        notified=notified,
    )
