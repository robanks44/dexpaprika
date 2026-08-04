"""Daily "all is well" position digest to ntfy (S13).

Replaces the old once-a-day manual check. Reuses the S12b read layer for content
and the S8 NtfyClient for delivery. HONEST: the digest reports "all clear" ONLY
when data is fresh AND the position is healthy — stale or missing data is called
out and downgrades the digest to "attention" (never a fabricated green).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import BaseModel

from dexpaprika.config import Settings
from dexpaprika.dashboard.read import derived, latest_view
from dexpaprika.secrets import resolve_provider

ClientFactory = Callable[[str], httpx.Client]

# Concern thresholds (percent). analyze() already flags SL at ≤3% and range edge ≤2%.
_NEAR_SL_PCT = Decimal(3)
_NEAR_FLOOR_PCT = Decimal(5)


class Digest(BaseModel):
    title: str
    message: str
    all_ok: bool
    priority: str
    tags: list[str]
    concerns: list[str]


class DigestResult(BaseModel):
    sent: bool
    reason: str | None
    digest: Digest


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def build_digest(conn: sqlite3.Connection, settings: Settings, *, now: datetime) -> Digest:
    """Compose the daily digest from the latest recorded state (read-only)."""
    lv = latest_view(conn, now=now)
    dv = derived(conn, settings)
    lp = lv.sources["lp"]
    hedge = lv.sources["hedge"]
    lines: list[str] = []
    concerns: list[str] = []

    if lp.stale:
        concerns.append("LP data stale")
    if hedge.stale:
        concerns.append("hedge data stale")

    lp_state = lp.entries[0]["state"] if lp.entries else None
    if lp_state:
        in_range = lp_state.get("in_range")
        price = lp_state.get("price_usd")
        lines.append(f"LP: {'in range' if in_range else 'OUT OF RANGE'} @ ${price}")
        if in_range is False:
            concerns.append("LP out of range")
    else:
        lines.append("LP: no open position")
        concerns.append("no LP position")

    if dv is not None and dv.analysis is not None:
        a = dv.analysis
        cov = a.get("coverage_ratio_eth")
        lp_delta = _dec(a.get("lp_delta_eth")) or Decimal(0)
        short_size = _dec(a.get("short_size_eth")) or Decimal(0)
        net_delta = lp_delta - short_size
        lines.append(
            f"Hedge: coverage {cov if cov is not None else 'n/a'}×,"
            f" net Δ {net_delta:.4f} ETH, quadrant {a.get('quadrant', '?')}"
        )
        dsl = _dec(a.get("distance_to_sl_pct"))
        if dsl is not None:
            lines.append(f"dist to SL: {dsl}%")
            if abs(dsl) < _NEAR_SL_PCT:
                concerns.append(f"near stop-loss ({dsl}%)")
        dfl = _dec(a.get("distance_to_floor_pct"))
        if dfl is not None and abs(dfl) < _NEAR_FLOOR_PCT:
            concerns.append(f"near range floor ({dfl}%)")
        # Rebalance drift is normal operating info (the strategy tracks it), NOT a
        # danger that flips the daily all-clear — surface it as a line, not a concern.
        lines.append(f"rebalance: {'NEEDED' if a.get('rebalance_needed') else 'ok'}")
        rr = dv.funding_run_rate_usd_per_day
        lines.append(f"funding/day: {('$' + rr) if rr is not None else dv.funding_run_rate_reason}")
        if dv.hedge_upnl_usd is not None:
            lines.append(f"hedge uPnL: ${dv.hedge_upnl_usd}")
    else:
        lines.append("Hedge/derived: unavailable (no priced LP + hedge)")
        concerns.append("no derived metrics")

    all_ok = not concerns
    title = "dexpaprika — all clear ✅" if all_ok else "dexpaprika — attention ⚠️"
    tags = ["white_check_mark"] if all_ok else ["warning"]
    priority = "low" if all_ok else "default"
    message = "\n".join(lines)
    if concerns:
        message += "\n\nConcerns: " + "; ".join(concerns)
    return Digest(
        title=title, message=message, all_ok=all_ok, priority=priority, tags=tags, concerns=concerns
    )


def send_digest(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    now: datetime,
    client_factory: ClientFactory | None = None,
) -> DigestResult:
    """Build + deliver the digest via ntfy. Unset topic → sent=False (surfaced)."""
    from dexpaprika.alerts.ntfy import NtfyClient

    digest = build_digest(conn, settings, now=now)
    topic = resolve_provider(settings).get("ntfy_topic")
    if topic is None:
        return DigestResult(sent=False, reason="ntfy_topic not configured", digest=digest)
    if client_factory is None:

        def _factory(base_url: str) -> httpx.Client:
            return httpx.Client(
                base_url=base_url, timeout=30.0, headers={"User-Agent": "dexpaprika/1.0"}
            )

        client_factory = _factory
    client = NtfyClient(
        conn, settings=settings, client=client_factory(settings.ntfy_server), topic=topic
    )
    client.publish(digest.title, digest.message, priority=digest.priority, tags=digest.tags)
    return DigestResult(sent=True, reason=None, digest=digest)
