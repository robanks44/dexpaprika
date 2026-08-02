"""Append-only lifecycle events derived from successive observations (S6).

Pattern from defi-portfolio best practices §3: positions are immutable
event streams, not mutable state. Every snapshot appends an ``observed``
row; this module derives the TRANSITION events (open / modify /
full_close) by comparing consecutive observed states. Nothing is ever
mutated or deleted; a full replay of open + modify deltas reconstructs
the final state (property-tested).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

# The metric per state shape whose change constitutes a 'modify'.
_TRACKED_FIELDS = ("liquidity", "size_tokens", "amount", "amount_usd", "total_collateral_usd")


def _tracked(state: dict[str, Any]) -> tuple[str, Any] | None:
    for field in _TRACKED_FIELDS:
        if field in state:
            return field, state[field]
    return None


def observe(conn: sqlite3.Connection, position_id: int, ts: str) -> str | None:
    """Derive the transition implied by the newest observed state, if any.

    Returns the derived event type ('open' / 'modify') or None.
    """
    rows = conn.execute(
        "SELECT state_json FROM position_events WHERE position_id=? AND type='observed'"
        " ORDER BY id DESC LIMIT 2",
        (position_id,),
    ).fetchall()
    if not rows:
        return None
    newest = json.loads(rows[0]["state_json"])
    if len(rows) == 1:
        conn.execute(
            "INSERT INTO position_events (position_id, ts, type, delta_json, state_json)"
            " VALUES (?, ?, 'open', '{}', ?)",
            (position_id, ts, json.dumps(newest)),
        )
        return "open"
    previous = json.loads(rows[1]["state_json"])
    new_metric = _tracked(newest)
    old_metric = _tracked(previous)
    if new_metric is None or old_metric is None:
        return None
    field, new_value = new_metric
    _old_field, old_value = old_metric
    if new_value == old_value:
        return None
    delta = {"field": field, "old": old_value, "new": new_value}
    conn.execute(
        "INSERT INTO position_events (position_id, ts, type, delta_json, state_json)"
        " VALUES (?, ?, 'modify', ?, ?)",
        (position_id, ts, json.dumps(delta), json.dumps(newest)),
    )
    return "modify"


def reconcile_closures(
    conn: sqlite3.Connection,
    wallet: str,
    venue: str,
    kind: str,
    present_external_ids: Sequence[str],
    ts: str,
) -> list[str]:
    """Open rows of (wallet, venue, kind) absent from the sweep → full_close once."""
    rows = conn.execute(
        "SELECT id, external_id FROM positions"
        " WHERE wallet_ref=? AND venue=? AND kind=? AND closed_at IS NULL",
        (wallet, venue, kind),
    ).fetchall()
    present = {external_id.lower() for external_id in present_external_ids}
    closed: list[str] = []
    for row in rows:
        if row["external_id"].lower() in present:
            continue
        conn.execute(
            "INSERT INTO position_events (position_id, ts, type, delta_json, state_json)"
            " VALUES (?, ?, 'full_close', '{}', '{}')",
            (row["id"], ts),
        )
        conn.execute("UPDATE positions SET closed_at=? WHERE id=?", (ts, row["id"]))
        closed.append(row["external_id"])
    return closed
