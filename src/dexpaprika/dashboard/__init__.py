"""Live dashboard + SSE over the recorder's SQLite store (S12b).

Read-only: the dashboard never calls upstream APIs — it reads only the DB the
recorder wrote, and the SSE trigger is a local DB watch. Derived metrics are
computed at read time from RAW rows. Charts use ECharts vendored locally.
"""

from __future__ import annotations

from dexpaprika.dashboard.app import RouteResult, route
from dexpaprika.dashboard.export import render_export
from dexpaprika.dashboard.read import (
    DerivedView,
    HistoryPoint,
    LatestView,
    SourcePanel,
    derived,
    history,
    latest_view,
)
from dexpaprika.dashboard.server import Broadcaster, DbWatcher, serve

__all__ = [
    "Broadcaster",
    "DbWatcher",
    "DerivedView",
    "HistoryPoint",
    "LatestView",
    "RouteResult",
    "SourcePanel",
    "derived",
    "history",
    "latest_view",
    "render_export",
    "route",
    "serve",
]
