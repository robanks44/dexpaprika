"""Static self-contained dashboard export (S12b secondary path).

Renders ONE standalone HTML snapshot: latest + derived + the chartable histories
inlined as ``window.__BOOTSTRAP__``, and the vendored ECharts decompressed and
inlined as a <script>. Opens offline with charts intact; makes zero requests.
"""

from __future__ import annotations

import gzip
import sqlite3
from datetime import datetime

from dexpaprika.config import Settings
from dexpaprika.dashboard import app, html, read

# (kind, field) pairs the page charts — kept in sync with html.py's loadHistory.
_EXPORT_SERIES: tuple[tuple[str, str], ...] = (
    ("lp", "price_usd"),
    ("perp", "mark_price"),
    ("perp", "size_tokens"),
    ("lp", "pool_volume_usd_24h"),
    ("perp", "pending_funding_fees_usd"),
)


def render_export(
    conn: sqlite3.Connection, settings: Settings, *, now: datetime | None = None
) -> str:
    """Standalone HTML snapshot of the current view."""
    latest = read.latest_view(conn, now=now).model_dump(mode="json")
    dv = read.derived(conn, settings)
    derived = None if dv is None else dv.model_dump(mode="json")
    histories: dict[str, list[dict[str, str | None]]] = {}
    for kind, field in _EXPORT_SERIES:
        try:
            points = read.history(conn, kind=kind, field=field)
        except ValueError:
            points = []
        histories[f"{kind}.{field}"] = [p.model_dump(mode="json") for p in points]
    echarts_js = gzip.decompress(app.echarts_gzip_bytes()).decode("utf-8")
    bootstrap = {"latest": latest, "derived": derived, "history": histories}
    return html.render_export(bootstrap, echarts_js)
