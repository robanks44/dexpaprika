"""Dashboard read layer — read-only over the recorder's SQLite store (S12b).

NO network, NO upstream client — every function reads only the DB the recorder
already wrote. Derived metrics are computed here at read time from RAW rows
(S12a); nothing derived is stored. Honest staleness: each source carries its own
last-good `as_of` + staleness; a source with no data reads "no data", not fresh.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from dexpaprika.config import Settings

# Whitelisted chartable fields per source kind — the client cannot pull an
# arbitrary JSON path (only these are exposed by /api/history).
HISTORY_FIELDS: dict[str, frozenset[str]] = {
    "lp": frozenset(
        {
            "price_usd",
            "token0_price_usd",
            "token1_price_usd",
            "pool_volume_usd_24h",
            "liquidity",
            "amount0",
            "amount1",
            "tokens_owed0",
            "tokens_owed1",
            "pool_tick",
        }
    ),
    "perp": frozenset(
        {
            "mark_price",
            "entry_price",
            "liquidation_price",
            "size_usd",
            "size_tokens",
            "collateral_usd",
            "leverage",
            "pnl",
            "pending_funding_fees_usd",
            "pending_borrowing_fees_usd",
        }
    ),
}

DEFAULT_STALE_AFTER_S = 300.0


class SourcePanel(BaseModel):
    kind: str
    as_of: str | None
    staleness_seconds: float | None
    stale: bool
    entries: list[dict[str, Any]]


class LatestView(BaseModel):
    now: str
    sources: dict[str, SourcePanel]


class HistoryPoint(BaseModel):
    ts: str
    value: str | None


class DerivedView(BaseModel):
    analysis: dict[str, Any] | None
    hedge_upnl_usd: str | None
    funding_run_rate_usd_per_day: str | None
    funding_run_rate_reason: str | None
    combined_pnl_usd: str | None
    combined_pnl_reason: str | None


def _source_staleness(
    conn: sqlite3.Connection, now: datetime
) -> dict[str, tuple[str | None, float | None]]:
    """Newest snapshot ts per source kind → (as_of, staleness_seconds)."""
    rows = conn.execute("SELECT kind, MAX(ts) AS ts FROM snapshots GROUP BY kind").fetchall()
    out: dict[str, tuple[str | None, float | None]] = {}
    for row in rows:
        ts = row["ts"]
        stale_s = (now - datetime.fromisoformat(ts)).total_seconds() if ts else None
        out[row["kind"]] = (ts, stale_s)
    return out


# snapshots.kind → position.kind(s) that make up that panel
_PANEL_POSITION_KINDS: dict[str, tuple[str, ...]] = {
    "lp": ("lp",),
    "hedge": ("perp",),
    "defi": ("lend", "borrow"),
    "holdings": ("holding",),
}


def _latest_entries(
    conn: sqlite3.Connection, position_kinds: tuple[str, ...]
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in position_kinds)
    rows = conn.execute(
        f"SELECT p.kind, p.external_id, e.ts AS as_of, e.state_json"  # noqa: S608 — kinds are fixed literals
        " FROM positions p"
        " JOIN position_events e ON e.position_id = p.id AND e.type='observed'"
        " WHERE p.closed_at IS NULL"
        f" AND p.kind IN ({placeholders})"
        " AND e.id = (SELECT MAX(id) FROM position_events"
        "             WHERE position_id = p.id AND type='observed')",
        position_kinds,
    ).fetchall()
    entries: list[dict[str, Any]] = []
    for row in rows:
        state = json.loads(row["state_json"])
        entries.append(
            {
                "kind": row["kind"],
                "external_id": row["external_id"],
                "as_of": row["as_of"],
                "state": state,
            }
        )
    return entries


def latest_view(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
) -> LatestView:
    """Per-source latest raw state + honest staleness (read-only)."""
    ref = now or datetime.now(UTC)
    staleness = _source_staleness(conn, ref)
    sources: dict[str, SourcePanel] = {}
    for panel_kind, position_kinds in _PANEL_POSITION_KINDS.items():
        as_of, stale_s = staleness.get(panel_kind, (None, None))
        sources[panel_kind] = SourcePanel(
            kind=panel_kind,
            as_of=as_of,
            staleness_seconds=stale_s,
            stale=(stale_s is None) or (stale_s > stale_after_s),
            entries=_latest_entries(conn, position_kinds),
        )
    return LatestView(now=ref.isoformat(), sources=sources)


def history(
    conn: sqlite3.Connection,
    *,
    kind: str,
    field: str,
    since: str | None = None,
    limit: int = 500,
) -> list[HistoryPoint]:
    """Time-series of one WHITELISTED field for a source kind (read-only).

    Raises ValueError on a non-whitelisted (kind, field) — the client can never
    pull an arbitrary JSON path.
    """
    allowed = HISTORY_FIELDS.get(kind)
    if allowed is None or field not in allowed:
        msg = f"field {field!r} not chartable for kind {kind!r}"
        raise ValueError(msg)
    # HISTORY_FIELDS keys ("lp", "perp") ARE position kinds — no indirection.
    params: list[Any] = [kind]
    since_clause = ""
    if since:
        since_clause = " AND e.ts >= ?"
        params.append(since)
    params.append(limit)
    rows = conn.execute(
        "SELECT e.ts AS ts, e.state_json"  # noqa: S608 — kind is a bound ?, since_clause is a fixed literal
        " FROM positions p JOIN position_events e ON e.position_id = p.id AND e.type='observed'"
        f" WHERE p.kind = ?{since_clause}"
        " ORDER BY e.id ASC LIMIT ?",
        params,
    ).fetchall()
    points: list[HistoryPoint] = []
    for row in rows:
        state = json.loads(row["state_json"])
        raw = state.get(field)
        points.append(HistoryPoint(ts=row["ts"], value=None if raw is None else str(raw)))
    return points


def _latest_perp_state(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT e.state_json FROM positions p"
        " JOIN position_events e ON e.position_id = p.id AND e.type='observed'"
        " WHERE p.kind='perp' AND p.closed_at IS NULL ORDER BY e.id DESC LIMIT 1"
    ).fetchone()
    return json.loads(row["state_json"]) if row else None


def _funding_run_rate(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """(usd_per_day, reason) from the last two hedge funding-fee samples."""
    rows = conn.execute(
        "SELECT e.ts, e.state_json FROM positions p"
        " JOIN position_events e ON e.position_id = p.id AND e.type='observed'"
        " WHERE p.kind='perp' AND p.closed_at IS NULL ORDER BY e.id DESC LIMIT 2"
    ).fetchall()
    if len(rows) < 2:
        return None, "needs ≥ 2 hedge samples"
    new = json.loads(rows[0]["state_json"])
    old = json.loads(rows[1]["state_json"])
    f_new = new.get("pending_funding_fees_usd")
    f_old = old.get("pending_funding_fees_usd")
    if f_new is None or f_old is None:
        return None, "funding fee not recorded in both samples"
    dt = (
        datetime.fromisoformat(rows[0]["ts"]) - datetime.fromisoformat(rows[1]["ts"])
    ).total_seconds()
    if dt <= 0:
        return None, "non-positive sample interval"
    rate = (Decimal(str(f_new)) - Decimal(str(f_old))) / Decimal(str(dt)) * Decimal(86400)
    return str(rate), None


def derived(conn: sqlite3.Connection, settings: Settings) -> DerivedView | None:
    """Derived-metrics section — reuses hedge.engine.analyze; adds run-rate + PnL.

    None when there is no priced LP observation (nothing to derive). Any metric
    whose inputs are missing is null-with-reason, never a fabricated number.
    """
    from dexpaprika.hedge.engine import analyze
    from dexpaprika.hedge.state import latest_inputs

    inputs = latest_inputs(conn)
    if inputs is None:
        return None
    lp, short, price = inputs
    analysis = analyze(lp, short, price, settings=settings).model_dump(mode="json")

    perp = _latest_perp_state(conn)
    hedge_upnl = None if perp is None else (None if perp.get("pnl") is None else str(perp["pnl"]))
    run_rate, run_reason = _funding_run_rate(conn)

    # Combined LP+hedge PnL since entry is NOT derivable — LP entry cost basis is
    # not recorded (only current value). Honest null-with-reason, never faked.
    combined_pnl = None
    combined_reason = (
        "LP entry cost basis not recorded; hedge uPnL + LP current value shown separately"
    )
    return DerivedView(
        analysis=analysis,
        hedge_upnl_usd=hedge_upnl,
        funding_run_rate_usd_per_day=run_rate,
        funding_run_rate_reason=run_reason,
        combined_pnl_usd=combined_pnl,
        combined_pnl_reason=combined_reason,
    )
