"""Latest recorded hedge inputs from the position event stream (S7/S8 shared)."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

from dexpaprika.hedge.engine import LpParams, ShortParams


def latest_inputs(conn: sqlite3.Connection) -> tuple[LpParams, ShortParams | None, Decimal] | None:
    """(LpParams, ShortParams|None, price) from the latest recorded states.

    None when no open LP position (or no priced observation) is recorded —
    callers decide whether that is an error (``hedge status``) or an alert
    input (``alerts check``: staleness/naked rules).
    """
    lp_row = conn.execute(
        "SELECT e.state_json FROM positions p"
        " JOIN position_events e ON e.position_id = p.id AND e.type='observed'"
        " WHERE p.kind='lp' AND p.closed_at IS NULL"
        " ORDER BY e.id DESC LIMIT 1"
    ).fetchone()
    if lp_row is None:
        return None
    lp_state = json.loads(lp_row["state_json"])
    if lp_state.get("price_usd") is None:
        return None
    lp = LpParams(
        tick_lower=lp_state["tick_lower"],
        tick_upper=lp_state["tick_upper"],
        liquidity=lp_state["liquidity"],
    )
    price = Decimal(str(lp_state["price_usd"]))

    short = None
    perp_row = conn.execute(
        "SELECT e.state_json FROM positions p"
        " JOIN position_events e ON e.position_id = p.id AND e.type='observed'"
        " WHERE p.kind='perp' AND p.closed_at IS NULL"
        " ORDER BY e.id DESC LIMIT 1"
    ).fetchone()
    if perp_row is not None:
        perp = json.loads(perp_row["state_json"])
        triggers = perp.get("stop_loss_triggers") or []
        short = ShortParams(
            size_eth=Decimal(str(perp["size_tokens"])),
            entry_price=Decimal(str(perp["entry_price"])),
            sl_trigger=Decimal(str(triggers[0])) if triggers else None,
            collateral_usd=(
                Decimal(str(perp["collateral_usd"])) if perp.get("collateral_usd") else None
            ),
        )
    return lp, short, price
