"""Pure dashboard router (S12b) — every non-SSE route resolves here.

Kept free of sockets so the whole request surface is unit-tested by calling
``route(...)`` directly. The stdlib server (server.py) is a thin adapter that
maps HTTP → ``route`` and streams ``/events`` separately.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from typing import Any

from dexpaprika.config import Settings
from dexpaprika.dashboard import html, read


@dataclass
class RouteResult:
    status: int
    content_type: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


def _json(payload: Any, status: int = 200) -> RouteResult:
    return RouteResult(
        status, "application/json; charset=utf-8", json.dumps(payload, default=str).encode()
    )


def echarts_gzip_bytes() -> bytes:
    """The vendored, gzip-compressed ECharts asset (served as-is with gzip encoding)."""
    return (resources.files("dexpaprika.dashboard.static") / "echarts.min.js.gz").read_bytes()


def route(
    path: str,
    query: dict[str, str],
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> RouteResult:
    """Resolve a GET path to a response. Read-only; never calls upstream."""
    if path == "/":
        return RouteResult(200, "text/html; charset=utf-8", html.render_page().encode())
    if path == "/static/echarts.min.js":
        return RouteResult(
            200,
            "application/javascript; charset=utf-8",
            echarts_gzip_bytes(),
            {"Content-Encoding": "gzip", "Cache-Control": "max-age=86400"},
        )
    if path == "/api/latest":
        return _json(read.latest_view(conn, now=now).model_dump(mode="json"))
    if path == "/api/derived":
        view = read.derived(conn, settings)
        return _json(None if view is None else view.model_dump(mode="json"))
    if path == "/api/history":
        kind = query.get("kind", "")
        field_name = query.get("field", "")
        since = query.get("since")
        try:
            points = read.history(conn, kind=kind, field=field_name, since=since)
        except ValueError as exc:
            return _json({"error": str(exc)}, status=400)
        return _json([p.model_dump(mode="json") for p in points])
    return _json({"error": "not found", "path": path}, status=404)
