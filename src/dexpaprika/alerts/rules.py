"""Alert rules engine over recorded state (S8 spec table).

Pure reads: the engine looks only at the shared database (plus the offline
health results the CLI passes in) and returns alerts — no network. Delivery,
cooldown bookkeeping, and the alerts_log audit trail live here too so a
firing is recorded BEFORE any delivery attempt: a dead ntfy server can lose
a notification, never the record of why it fired.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from dexpaprika.config import Settings
from dexpaprika.hedge.engine import HedgeAnalysis, analyze
from dexpaprika.hedge.state import latest_inputs
from dexpaprika.quota import QuotaTracker


class Alert(BaseModel):
    """One rule firing, ready to record and deliver."""

    rule: str
    severity: str  # doubles as the ntfy priority name
    title: str
    message: str
    firing_inputs: dict[str, Any]
    tags: list[str] = []


def _num(value: Decimal | None, places: str = "0.0001") -> str | None:
    return None if value is None else str(value.quantize(Decimal(places)))


def _hedge_alerts(conn: sqlite3.Connection, settings: Settings) -> list[Alert]:
    inputs = latest_inputs(conn)
    if inputs is None:
        return []
    lp, short, price = inputs
    analysis: HedgeAnalysis = analyze(lp, short, price, settings=settings)
    base_inputs: dict[str, Any] = {
        "price_usd": _num(analysis.price_usd),
        "quadrant": analysis.quadrant,
        "lp_delta_eth": _num(analysis.lp_delta_eth),
        "short_size_eth": _num(analysis.short_size_eth),
        "coverage_ratio_eth": _num(analysis.coverage_ratio_eth),
    }
    alerts: list[Alert] = []
    if "naked-lp" in analysis.flags:
        alerts.append(
            Alert(
                rule="naked-lp",
                severity="urgent",
                title="HEDGE: naked LP",
                message=(
                    f"No short leg against {_num(analysis.lp_delta_eth)} ETH of LP delta"
                    f" — downside is unhedged (price ${_num(analysis.price_usd, '0.01')})."
                ),
                firing_inputs=base_inputs,
                tags=["rotating_light"],
            )
        )
    if "price-near-sl" in analysis.flags:
        alerts.append(
            Alert(
                rule="price-near-sl",
                severity="urgent",
                title="HEDGE: price near stop-loss",
                message=(
                    f"Price ${_num(analysis.price_usd, '0.01')} is"
                    f" {_num(analysis.distance_to_sl_pct, '0.01')}% from the SL trigger"
                    " — a stop-out re-exposes the LP downside."
                ),
                firing_inputs={
                    **base_inputs,
                    "distance_to_sl_pct": _num(analysis.distance_to_sl_pct),
                },
                tags=["rotating_light", "chart_with_downwards_trend"],
            )
        )
    if "near-band-edge" in analysis.flags:
        alerts.append(
            Alert(
                rule="near-band-edge",
                severity="high",
                title="LP: price near band edge",
                message=(
                    f"Price ${_num(analysis.price_usd, '0.01')} —"
                    f" {_num(analysis.distance_to_floor_pct, '0.01')}% above floor,"
                    f" {_num(analysis.distance_to_ceiling_pct, '0.01')}% below ceiling"
                    " (quadrant decision points apply)."
                ),
                firing_inputs={
                    **base_inputs,
                    "distance_to_floor_pct": _num(analysis.distance_to_floor_pct),
                    "distance_to_ceiling_pct": _num(analysis.distance_to_ceiling_pct),
                },
                tags=["warning"],
            )
        )
    if analysis.rebalance_needed:
        alerts.append(
            Alert(
                rule="rebalance-needed",
                severity="high",
                title="HEDGE: coverage drift beyond band",
                message=(
                    f"Short {_num(analysis.short_size_eth)} ETH vs delta-matched target"
                    f" {_num(analysis.delta_matched_target_eth)} ETH — deviation exceeds"
                    f" the {settings.hedge_rebalance_band} band"
                    " (recommendation only; execution is S9-gated)."
                ),
                firing_inputs={
                    **base_inputs,
                    "delta_matched_target_eth": _num(analysis.delta_matched_target_eth),
                    "break_even_short_size": _num(analysis.break_even_short_size),
                    "flags": analysis.flags,
                },
                tags=["warning", "scales"],
            )
        )
    return alerts


def _staleness_alert(conn: sqlite3.Connection, settings: Settings, now: datetime) -> Alert | None:
    row = conn.execute("SELECT MAX(ts) AS newest FROM snapshots").fetchone()
    newest = row["newest"]
    age_minutes: int | None = None
    if newest is not None:
        age = now - datetime.fromisoformat(newest)
        age_minutes = int(age.total_seconds() // 60)
        if age_minutes <= settings.snapshot_staleness_minutes:
            return None
        detail = f"newest snapshot is {age_minutes} min old"
    else:
        detail = "no snapshots recorded at all"
    return Alert(
        rule="snapshot-stale",
        severity="high",
        title="RECORDER: snapshots stale",
        message=(
            f"{detail} (threshold {settings.snapshot_staleness_minutes} min)"
            " — the scheduled recorder is not landing data."
        ),
        firing_inputs={"newest_ts": newest, "age_minutes": age_minutes},
        tags=["warning", "hourglass"],
    )


def _quota_alerts(conn: sqlite3.Connection, settings: Settings, now: datetime) -> list[Alert]:
    tracker = QuotaTracker(conn, now=lambda: now)
    alerts: list[Alert] = []
    for summary in tracker.summaries():
        limit = summary["credit_limit"]
        if not limit:  # rate windows fill transiently by design — never alerted
            continue
        used = Decimal(summary["month_credits"]) / Decimal(int(limit))
        if used >= settings.quota_alert_used_pct:
            alerts.append(
                Alert(
                    rule="quota-critical",
                    severity="high",
                    title=f"QUOTA: {summary['provider']} monthly budget",
                    message=(
                        f"{summary['provider']}: {summary['month_credits']}/{limit} monthly"
                        f" credits used ({used:.0%}) — throttle or raise the budget."
                    ),
                    firing_inputs=dict(summary),
                    tags=["warning", "fuelpump"],
                )
            )
    return alerts


def _health_alert(health: Mapping[str, str]) -> Alert | None:
    failed = {name: result for name, result in health.items() if result != "ok"}
    if not failed:
        return None
    return Alert(
        rule="healthcheck-degraded",
        severity="high",
        title="SYSTEM: healthcheck degraded",
        message="Failing checks: " + ", ".join(sorted(failed)) + ".",
        firing_inputs={"failed": failed},
        tags=["warning", "stethoscope"],
    )


def evaluate(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    now: datetime | None = None,
    health: Mapping[str, str] | None = None,
) -> list[Alert]:
    """Run every rule over recorded state; returns firings (pre-cooldown)."""
    moment = now or datetime.now(UTC)
    alerts = _hedge_alerts(conn, settings)
    staleness = _staleness_alert(conn, settings, moment)
    if staleness is not None:
        alerts.append(staleness)
    alerts.extend(_quota_alerts(conn, settings, moment))
    if health is not None:
        health_alert = _health_alert(health)
        if health_alert is not None:
            alerts.append(health_alert)
    return alerts


# ------------------------------ bookkeeping ------------------------------


def apply_cooldown(
    conn: sqlite3.Connection,
    alerts: list[Alert],
    *,
    settings: Settings,
    now: datetime,
) -> tuple[list[Alert], list[Alert]]:
    """Split into (fire, suppressed) using alerts_log recency per rule.

    Undelivered firings cool down too — a dead channel must not turn into
    a retry storm against the free ntfy server.
    """
    window = timedelta(minutes=settings.alert_cooldown_minutes)
    fire: list[Alert] = []
    suppressed: list[Alert] = []
    for alert in alerts:
        row = conn.execute(
            "SELECT MAX(ts) AS last FROM alerts_log WHERE rule = ?", (alert.rule,)
        ).fetchone()
        last = row["last"]
        if last is not None and now - datetime.fromisoformat(last) < window:
            suppressed.append(alert)
        else:
            fire.append(alert)
    return fire, suppressed


def record_alert(conn: sqlite3.Connection, alert: Alert, *, now: datetime) -> int:
    """Append the firing (delivered=0) BEFORE any delivery attempt."""
    cursor = conn.execute(
        "INSERT INTO alerts_log (ts, rule, severity, payload_json, delivered)"
        " VALUES (?, ?, ?, ?, 0)",
        (now.isoformat(), alert.rule, alert.severity, json.dumps(alert.firing_inputs)),
    )
    return int(cursor.lastrowid or 0)


def mark_delivery(
    conn: sqlite3.Connection, alert_id: int, *, delivered: bool, ntfy_status: str
) -> None:
    conn.execute(
        "UPDATE alerts_log SET delivered = ?, ntfy_status = ? WHERE id = ?",
        (1 if delivered else 0, ntfy_status, alert_id),
    )
