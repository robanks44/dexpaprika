"""The S9 gate chain (ENGINEERING_STANDARDS §4) — all checks BEFORE the client.

Kill switch: a FILE in the data dir. Code can create it (auto-trip) and
check it; NO code path removes it — manual re-arm only, which is what
keeps it outside the agent's control. `execute arm` refuses while it
exists.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from dexpaprika.config import Settings

KILL_SWITCH_FILE = "KILL-SWITCH"
ARMED_FILE = "ARMED"
CONSECUTIVE_FAILURES_TO_TRIP = 3


class GateResult(BaseModel, frozen=True):
    allowed: bool
    reason: str | None = None


def _kill_path(settings: Settings) -> Path:
    return settings.data_dir / KILL_SWITCH_FILE


def _armed_path(settings: Settings) -> Path:
    return settings.data_dir / ARMED_FILE


def check_kill_switch(settings: Settings) -> GateResult:
    path = _kill_path(settings)
    if path.exists():
        return GateResult(
            allowed=False,
            reason=(
                f"kill switch present at {path} — all mutating behaviour halted;"
                " manual removal by Richard is the ONLY re-arm path"
            ),
        )
    return GateResult(allowed=True)


def trip_kill_switch(
    settings: Settings, conn: sqlite3.Connection, reason: str, *, now: datetime
) -> None:
    """Create the switch (idempotent) and audit the trip. Never removed by code."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = _kill_path(settings)
    entry = f"{now.isoformat()} TRIPPED: {reason}\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry)
    conn.execute(
        "INSERT INTO audit_log (ts, actor, action, phase, payload_json)"
        " VALUES (?, 'executor', 'kill-switch-trip', 'blocked', ?)",
        (now.isoformat(), json.dumps({"reason": reason})),
    )


def arm(settings: Settings, *, ttl_minutes: int | None = None, now: datetime) -> Path:
    """The separate arming step. Refuses while the kill switch exists."""
    gate = check_kill_switch(settings)
    if not gate.allowed:
        raise RuntimeError(gate.reason or "kill switch present")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    ttl = ttl_minutes if ttl_minutes is not None else settings.arm_ttl_minutes
    expires = now + timedelta(minutes=ttl)
    path = _armed_path(settings)
    path.write_text(
        json.dumps({"armed_at": now.isoformat(), "expires_at": expires.isoformat()}) + "\n",
        encoding="utf-8",
    )
    return path


def check_armed(settings: Settings, *, arm_flag: bool, now: datetime) -> GateResult:
    """Live execution needs BOTH the --arm flag and a fresh armed-state file."""
    if not arm_flag:
        return GateResult(allowed=False, reason="dry-run (no --arm flag)")
    path = _armed_path(settings)
    if not path.exists():
        return GateResult(
            allowed=False,
            reason="--arm given but no armed-state file — run `dexpaprika execute arm` first",
        )
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        expires = datetime.fromisoformat(state["expires_at"])
    except (ValueError, KeyError, OSError) as exc:
        return GateResult(allowed=False, reason=f"armed-state file unreadable: {exc}")
    if now >= expires:
        return GateResult(
            allowed=False,
            reason=f"armed state expired at {expires.isoformat()} — re-run `execute arm`",
        )
    return GateResult(allowed=True)


def _recent_submissions(conn: sqlite3.Connection, since: datetime) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT ts FROM audit_log WHERE phase='submission' AND ts >= ? ORDER BY ts",
        (since.isoformat(),),
    ).fetchall()


def check_limits(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    now: datetime,
    market: str,
    new_position_usd: Decimal,
    delta_usd: Decimal,
) -> GateResult:
    """Hard limits in code, pre-client (§4) — venue settings are NOT the control."""
    if market not in settings.allowed_markets:
        return GateResult(
            allowed=False,
            reason=f"market {market!r} not in allowed_markets {settings.allowed_markets}",
        )
    if settings.max_position_usd > 0 and new_position_usd > settings.max_position_usd:
        return GateResult(
            allowed=False,
            reason=(
                f"position ${new_position_usd} exceeds max_position_usd"
                f" ${settings.max_position_usd}"
            ),
        )
    if settings.max_delta_per_run_usd > 0 and delta_usd > settings.max_delta_per_run_usd:
        return GateResult(
            allowed=False,
            reason=(
                f"delta ${delta_usd} exceeds max_delta_per_run_usd"
                f" ${settings.max_delta_per_run_usd}"
            ),
        )
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = _recent_submissions(conn, day_start)
    if settings.max_daily_adjustments > 0 and len(today) >= settings.max_daily_adjustments:
        return GateResult(
            allowed=False,
            reason=(
                f"daily adjustment cap reached ({len(today)}/{settings.max_daily_adjustments})"
            ),
        )
    # Queried independently of the daily list so the window spans midnight
    # (verifier finding #3).
    cutoff = now - timedelta(seconds=settings.order_rate_limit_seconds)
    window = [
        r for r in _recent_submissions(conn, cutoff) if datetime.fromisoformat(r["ts"]) > cutoff
    ]
    if window:
        return GateResult(
            allowed=False,
            reason=(
                f"submission rate limit: last order {window[-1]['ts']} is inside the"
                f" {settings.order_rate_limit_seconds}s window"
            ),
        )
    return GateResult(allowed=True)


def consecutive_submit_failures(conn: sqlite3.Connection) -> int:
    """Failed submissions since the last successful confirmation."""
    rows = conn.execute(
        "SELECT action, phase FROM audit_log WHERE phase IN ('confirmation', 'rejected')"
        " AND actor='executor' ORDER BY id DESC LIMIT 10"
    ).fetchall()
    count = 0
    for row in rows:
        if row["phase"] == "confirmation":
            break
        if row["action"] == "submit-failed":
            count += 1
    return count
