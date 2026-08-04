"""S12b — dashboard read layer, router, SSE broadcaster/watcher, export (offline).

Zero network, zero sockets: the DB is seeded directly; ``route``, ``Broadcaster``,
and ``DbWatcher`` are exercised in-process.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from dexpaprika.config import Settings
from dexpaprika.dashboard import app, export, read
from dexpaprika.dashboard import server as dashboard_server
from dexpaprika.dashboard.server import Broadcaster, DbWatcher
from dexpaprika.storage.db import connect
from dexpaprika.storage.migrations import migrate

T0 = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)

LP_STATE: dict[str, Any] = {
    "nfpm": "0xa990c6a764b73bf43cee5bb40339c3322fb9d55f",
    "token_id": 5056427,
    "tick_lower": -202000,
    "tick_upper": -200000,
    "liquidity": 3987414535131380,
    "amount0": "4.8192",
    "amount1": "7809.21",
    "tokens_owed0": 0,
    "tokens_owed1": 0,
    "pool_tick": -201069,
    "sqrt_price_x96": 3411467318683924956716100,
    "price_usd": "1854.05",
    "token0_price_usd": "1854.05",
    "token1_price_usd": "1",
    "pool_volume_usd_24h": "16542.33",
    "in_range": True,
}


def _perp_state(*, funding: str, pnl: str = "46.0") -> dict[str, Any]:
    return {
        "key": "0xperp",
        "account": "0xC155",
        "is_long": False,
        "size_usd": "1854.05",
        "size_tokens": "1.0",
        "entry_price": "1900.0",
        "mark_price": "1854.05",
        "liquidation_price": "2100.0",
        "collateral_usd": "1000.0",
        "collateral_amount": "1000.0",
        "leverage": "1.85",
        "pnl": pnl,
        "pending_funding_fees_usd": funding,
        "pending_borrowing_fees_usd": "0.5",
        "stop_loss_triggers": ["1901.0"],
        "stop_loss_orders": [{"trigger": "1901.0", "size_usd": None, "is_full_close": True}],
    }


def _add_position(conn: sqlite3.Connection, kind: str, external_id: str) -> int:
    conn.execute(
        "INSERT INTO positions (wallet_ref, venue, chain, kind, external_id, group_tag, opened_at)"
        " VALUES ('0xC155', ?, ?, ?, ?, 'lp_hedge', ?)",
        (
            "aerodrome" if kind == "lp" else "gmx",
            "base" if kind == "lp" else "arbitrum",
            kind,
            external_id,
            T0.isoformat(),
        ),
    )
    return int(
        conn.execute("SELECT id FROM positions WHERE external_id=?", (external_id,)).fetchone()[
            "id"
        ]
    )


def _observe(
    conn: sqlite3.Connection, position_id: int, ts: datetime, state: dict[str, Any]
) -> None:
    conn.execute(
        "INSERT INTO position_events (position_id, ts, type, delta_json, state_json)"
        " VALUES (?, ?, 'observed', '{}', ?)",
        (position_id, ts.isoformat(), json.dumps(state)),
    )


def _snapshot(conn: sqlite3.Connection, kind: str, ts: datetime, block: int | None = 1) -> None:
    chain = {"lp": "base", "hedge": "arbitrum"}.get(kind, "base")
    conn.execute(
        "INSERT INTO snapshots (ts, chain, block_number, kind) VALUES (?, ?, ?, ?)",
        (ts.isoformat(), chain, block, kind),
    )


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "dash.db")
    migrate(c)
    return c


def _seed_full(conn: sqlite3.Connection, *, two_hedge_samples: bool = True) -> None:
    lp_id = _add_position(conn, "lp", "lp:1")
    _observe(conn, lp_id, T0, LP_STATE)
    _snapshot(conn, "lp", T0)
    perp_id = _add_position(conn, "perp", "perp:1")
    _observe(conn, perp_id, T0 - timedelta(hours=1), _perp_state(funding="1.0"))
    if two_hedge_samples:
        _observe(conn, perp_id, T0, _perp_state(funding="3.0"))
    _snapshot(conn, "hedge", T0)


def _settings() -> Settings:
    return Settings.load()


# --------------------------- 1. latest_view ---------------------------


def test_latest_view_staleness_from_snapshot_ts(conn: sqlite3.Connection) -> None:
    _seed_full(conn)
    view = read.latest_view(conn, now=T0 + timedelta(seconds=120), stale_after_s=300)
    lp = view.sources["lp"]
    assert lp.as_of == T0.isoformat()
    assert lp.staleness_seconds == pytest.approx(120.0)
    assert lp.stale is False
    assert lp.entries and lp.entries[0]["state"]["liquidity"] == LP_STATE["liquidity"]
    # a source with no data reads not-fresh, never silently fresh
    assert view.sources["defi"].as_of is None
    assert view.sources["defi"].stale is True


def test_latest_view_stale_flips_past_threshold(conn: sqlite3.Connection) -> None:
    _seed_full(conn)
    fresh = read.latest_view(conn, now=T0 + timedelta(seconds=10), stale_after_s=300)
    stale = read.latest_view(conn, now=T0 + timedelta(seconds=600), stale_after_s=300)
    assert fresh.sources["lp"].stale is False
    assert stale.sources["lp"].stale is True


# --------------------------- 2. history ---------------------------


def test_history_whitelisted_field_ordered(conn: sqlite3.Connection) -> None:
    _seed_full(conn)
    pts = read.history(conn, kind="perp", field="pending_funding_fees_usd")
    assert [p.value for p in pts] == ["1.0", "3.0"]  # chronological (id ASC)


def test_history_rejects_non_whitelisted_field(conn: sqlite3.Connection) -> None:
    _seed_full(conn)
    with pytest.raises(ValueError, match="not chartable"):
        read.history(conn, kind="perp", field="account")  # not in whitelist
    with pytest.raises(ValueError, match="not chartable"):
        read.history(conn, kind="unknown", field="price_usd")


# --------------------------- 3. derived ---------------------------


def test_derived_matches_analyze_and_run_rate(conn: sqlite3.Connection) -> None:
    from dexpaprika.hedge.engine import analyze
    from dexpaprika.hedge.state import latest_inputs

    _seed_full(conn)
    dv = read.derived(conn, _settings())
    assert dv is not None
    inputs = latest_inputs(conn)
    assert inputs is not None
    lp, short, price = inputs
    assert dv.analysis == analyze(lp, short, price, settings=_settings()).model_dump(mode="json")
    assert dv.hedge_upnl_usd == "46.0"
    # funding run-rate: (3.0 - 1.0) over 3600s → 48 USD/day
    assert dv.funding_run_rate_usd_per_day is not None
    assert float(dv.funding_run_rate_usd_per_day) == pytest.approx(48.0, abs=1e-6)
    # combined PnL is honestly null-with-reason (no LP cost basis recorded)
    assert dv.combined_pnl_usd is None
    assert "cost basis" in (dv.combined_pnl_reason or "")


def test_derived_null_with_reason_when_one_hedge_sample(conn: sqlite3.Connection) -> None:
    _seed_full(conn, two_hedge_samples=False)
    dv = read.derived(conn, _settings())
    assert dv is not None
    assert dv.funding_run_rate_usd_per_day is None
    assert "≥ 2" in (dv.funding_run_rate_reason or "")


def test_derived_none_without_priced_lp(conn: sqlite3.Connection) -> None:
    assert read.derived(conn, _settings()) is None  # empty DB → nothing to derive


# --------------------------- 4. route ---------------------------


def test_route_api_endpoints(conn: sqlite3.Connection) -> None:
    _seed_full(conn)
    s = _settings()
    r = app.route("/api/latest", {}, conn, s, now=T0 + timedelta(seconds=30))
    assert r.status == 200 and r.content_type.startswith("application/json")
    body = json.loads(r.body)
    assert body["sources"]["lp"]["as_of"] == T0.isoformat()

    r = app.route("/api/derived", {}, conn, s)
    assert r.status == 200 and json.loads(r.body)["hedge_upnl_usd"] == "46.0"

    r = app.route("/api/history", {"kind": "perp", "field": "mark_price"}, conn, s)
    assert r.status == 200 and isinstance(json.loads(r.body), list)


def test_route_history_bad_field_400(conn: sqlite3.Connection) -> None:
    r = app.route("/api/history", {"kind": "perp", "field": "account"}, conn, _settings())
    assert r.status == 400
    assert "not chartable" in json.loads(r.body)["error"]


def test_route_root_html_has_no_external_urls(conn: sqlite3.Connection) -> None:
    r = app.route("/", {}, conn, _settings())
    assert r.status == 200 and r.content_type.startswith("text/html")
    page = r.body.decode()
    assert "http://" not in page and "https://" not in page  # self-contained; /static + /api only
    assert "/static/echarts.min.js" in page


def test_route_static_echarts_gzip(conn: sqlite3.Connection) -> None:
    r = app.route("/static/echarts.min.js", {}, conn, _settings())
    assert r.status == 200
    assert r.headers.get("Content-Encoding") == "gzip"
    assert r.body[:2] == b"\x1f\x8b"  # gzip magic
    assert b"echarts" in gzip.decompress(r.body)[:5000].lower()


def test_route_unknown_404(conn: sqlite3.Connection) -> None:
    r = app.route("/nope", {}, conn, _settings())
    assert r.status == 404


# --------------------------- 5. Broadcaster ---------------------------


def test_broadcaster_fanout_and_unsubscribe() -> None:
    b = Broadcaster()
    q1 = b.subscribe()
    q2 = b.subscribe()
    assert b.publish("update") == 2
    assert q1.get_nowait() == "update"
    assert q2.get_nowait() == "update"
    b.unsubscribe(q1)
    assert b.subscriber_count() == 1
    assert b.publish("update") == 1


# --------------------------- 6. DbWatcher ---------------------------


def test_db_watcher_publishes_on_new_snapshot(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _snapshot(conn, "lp", T0)  # baseline row
    b = Broadcaster()
    q = b.subscribe()
    watcher = DbWatcher(
        lambda: connect(tmp_path / "dash.db"),
        b,
        sleep=lambda _s: None,
        stop=lambda: False,
        interval=1.0,
    )
    _snapshot(conn, "hedge", T0 + timedelta(seconds=1))  # a new row appears
    published = watcher.run(max_ticks=1)
    assert published == 1
    assert q.get_nowait() == "update"
    # no further rows → no publish
    assert watcher.run(max_ticks=1) == 0


def test_db_watcher_stops_immediately_when_flagged(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    b = Broadcaster()
    watcher = DbWatcher(
        lambda: connect(tmp_path / "dash.db"),
        b,
        sleep=lambda _s: None,
        stop=lambda: True,
        interval=1.0,
    )
    assert watcher.run(max_ticks=10) == 0  # stop flag → no ticks


# --------------------------- 7. export ---------------------------


def test_export_is_self_contained(conn: sqlite3.Connection) -> None:
    _seed_full(conn)
    out = export.render_export(conn, _settings(), now=T0 + timedelta(seconds=30))
    # No external RESOURCE loads (a license URL in the inlined JS comment is fine).
    assert 'src="http' not in out
    assert 'href="http' not in out
    assert "//cdn" not in out
    assert "/static/echarts.min.js" not in out  # inlined, not linked
    assert "__BOOTSTRAP__" in out
    assert "echarts" in out.lower()  # ECharts inlined
    assert "1854.05" in out  # a real latest value is present


# --------------------------- 8. handler glue (no real socket) ---------------------------


class _FakeHandler(dashboard_server._Handler):
    """Drive _Handler.do_GET with in-memory rfile/wfile — no socket bound."""

    def __init__(self, path: str, headers: dict[str, str], server_obj: Any) -> None:
        import email.message
        import io

        self.path = path
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        msg = email.message.Message()
        for k, v in headers.items():
            msg[k] = v
        self.headers = msg
        self.server = server_obj
        self.client_address = ("127.0.0.1", 0)
        self.request_version = "HTTP/1.0"
        self.command = "GET"
        self.requestline = f"GET {path} HTTP/1.0"


def _drive(path: str, headers: dict[str, str], db: Path) -> tuple[int, dict[str, str], bytes]:
    from types import SimpleNamespace

    from dexpaprika.dashboard.server import Broadcaster

    server_obj = SimpleNamespace(
        conn_factory=lambda: connect(db), settings=_settings(), broadcaster=Broadcaster()
    )
    import io

    buf = io.BytesIO()  # typed BytesIO so .getvalue() is visible to mypy
    h = _FakeHandler(path, headers, server_obj)
    h.wfile = buf
    h.do_GET()
    raw = buf.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    hdrs = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
    return status, hdrs, body


def test_handler_serves_json_and_negotiates_gzip(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _seed_full(conn)
    db = tmp_path / "dash.db"
    # /api/latest → JSON
    status, _hdrs, body = _drive("/api/latest", {}, db)
    assert status == 200 and json.loads(body)["sources"]["lp"]["as_of"] == T0.isoformat()
    # gzip client → compressed bytes + Content-Encoding
    st_gz, h_gz, b_gz = _drive("/static/echarts.min.js", {"Accept-Encoding": "gzip"}, db)
    assert st_gz == 200 and h_gz.get("Content-Encoding") == "gzip" and b_gz[:2] == b"\x1f\x8b"
    # non-gzip client → decompressed, header dropped
    st_raw, h_raw, b_raw = _drive("/static/echarts.min.js", {}, db)
    assert st_raw == 200 and "Content-Encoding" not in h_raw and b_raw[:2] != b"\x1f\x8b"
    assert b"echarts" in b_raw[:5000].lower()
    # unknown path → 404
    assert _drive("/nope", {}, db)[0] == 404
