# S12b — Live dashboard + SSE push (spec)

**Status:** in_progress (branch `section/s12b-dashboard`)
**Depends:** S12a (recorder + raw store), S6 (report read seam), S7 (`hedge.engine.analyze`
derived metrics), S5 (range bounds).
**Framework decision (Richard, 2026-08-04):** stdlib `http.server` — zero new runtime deps,
sync (matches the httpx/sqlite codebase), local single-user dashboard. NOT Flask/FastAPI.

## Purpose

A LOCAL, read-only dashboard over the recorder's SQLite store: real-time latest view +
historical charts + a derived-metrics section, with honest per-panel staleness. The browser
NEVER calls upstream APIs — it reads this server, and the server reads only SQLite (+ the
recorder's heartbeat). One local feed → N viewers via SSE. A dead source LOOKS dead.

This section adds NO upstream calls and NO new runtime dependency. Derived metrics are
computed at read/display time from RAW rows (S12a) — nothing derived is stored.

## Public interface

- `dexpaprika.dashboard.read` — the pure read layer (read-only DB, no network):
  - `latest_view(conn) -> LatestView` — per-source latest raw state (LP, hedge, defi,
    holdings) + each source's `as_of` + `staleness_seconds` (from the newest `snapshots` /
    `recorder_heartbeat` row for that kind). Mirrors `_cmd_report`'s selection.
  - `history(conn, *, kind, field, since=None, limit=500) -> list[HistoryPoint]` — a
    time-series `(ts, value)` pulled from `position_events.state_json` for one field, for
    charts. Whitelisted fields only (no arbitrary JSON path from the client).
  - `derived(conn, settings) -> DerivedView | None` — the derived-metrics section. Reuses
    `hedge.state.latest_inputs` + `hedge.engine.analyze` (coverage ratio, net delta,
    distance-to-floor/ceiling/SL %, quadrant, rebalance_needed, premium-if-SL). Adds, at
    display time and only when the inputs exist (else null-with-reason, never fabricated):
    combined LP+hedge unrealized PnL, funding run-rate (Δ pending_funding across the last
    two hedge samples ÷ Δt), and fees-vs-IL when an entry reference is available.
- `dexpaprika.dashboard.app.route(path, query, conn, settings) -> RouteResult` — pure
  request router (status, content_type, body). Every HTTP route resolves through this so the
  logic is unit-tested with zero sockets. Routes:
  - `GET /` → the self-contained HTML shell (inline CSS + vanilla-JS). Charts use **Apache
    ECharts, VENDORED locally** (Richard, 2026-08-04) — the minified JS lives in
    `dashboard/static/` and is served by THIS server (`GET /static/echarts.min.js`); NO CDN,
    no network at view time. Charts: line/area price+size history, gauges for
    distance-to-SL / distance-to-liq / coverage, an in-range position bar. Design follows the
    `dataviz` skill (palette, dark/light, dense layout).
  - `GET /api/latest` → `latest_view` as JSON.
  - `GET /api/history?kind=&field=&since=` → `history` as JSON.
  - `GET /api/derived` → `derived` as JSON.
  - `GET /events` → SSE stream (handled in the server adapter, not `route`).
- `dexpaprika.dashboard.server` — the thin stdlib adapter:
  - `Broadcaster` — thread-safe subscriber registry; `publish(event)` fan-outs to all
    subscriber queues; `subscribe()/unsubscribe()`. Unit-tested without sockets.
  - `DbWatcher(conn_factory, broadcaster, *, sleep, stop, interval)` — a background loop
    that reads `MAX(id) FROM snapshots` (+ heartbeat) on its OWN read-only connection and
    `publish`es an `update` event when it advances. This is the SSE trigger — a LOCAL DB
    watch, never an upstream poll. Injected `sleep`/`stop` → deterministic tests.
  - `DashboardHandler` / `serve(conn_factory, settings, *, host, port, stop)` — binds
    `ThreadingHTTPServer`, dispatches non-SSE paths through `route`, streams `/events` from a
    `Broadcaster` subscription. Bind is `127.0.0.1` only.
- CLI:
  - `dexpaprika dashboard serve [--host 127.0.0.1] [--port 8787]` — run the server (reads
    the DB the recorder writes; typically launched beside `recorder run`).
  - `dexpaprika dashboard export [--out FILE]` — the secondary path: render ONE
    self-contained static HTML snapshot of the current latest+derived view (no server, no
    SSE) for sharing/archival. The vendored ECharts JS is INLINED into the file (`<script>…`)
    so the export is truly standalone — opens offline with charts intact, zero external refs.

## Behavioural rules

- **Read-only, no upstream.** The dashboard opens the DB read-only and never constructs an
  API client. The SSE trigger is a local DB watch. (A viewer refresh cannot cause an
  upstream call — the property that makes "one feed → N viewers" safe.)
- **Honest staleness (ENGINEERING_STANDARDS §2).** Every panel carries its source's
  `as_of` + `staleness_seconds`; the UI shows a stale badge past a threshold. Staleness is
  measured from the last GOOD data for that source (consistent with S12a `recorder status`),
  never reset by a failed cycle. A source with no data reads "no data", not "fresh".
- **No fabrication.** A derived metric whose inputs are missing (no short, no entry price,
  <2 hedge samples for a run-rate) is emitted as null with a reason string — never a
  placeholder number. Money stays Decimal end to end; JSON serializes as strings.
- **Correctness without the daemon.** The dashboard is LIVENESS only; it reads what the
  recorder/snapshot already persisted. It is never on the recording or alert path.

## Tests (offline, written first)

1. `latest_view`: from a seeded DB (reuse S6/S12a fixtures) returns each source's latest
   raw state + correct `staleness_seconds` from the newest snapshot ts (injected `now`).
2. `history`: returns ordered `(ts, value)` for a whitelisted field; rejects a non-whitelisted
   field; respects `since`/`limit`.
3. `derived`: matches hand-computed `hedge.engine.analyze` outputs on a seeded LP+hedge;
   combined PnL + funding run-rate correct; returns null-with-reason when short/samples absent.
4. `route`: `/api/latest|history|derived` return 200 + expected JSON; `/` returns 200 HTML
   with no external URLs; unknown path → 404; bad query → 400.
5. `Broadcaster`: publish reaches every subscriber; unsubscribe stops delivery; thread-safe
   under concurrent publish/subscribe.
6. `DbWatcher`: with injected sleep/stop + a fresh row inserted, publishes exactly one
   `update`; no row → no publish; terminates on stop.
7. Staleness badge logic: the latest-view staleness crosses the threshold → `stale=true`
   in the payload a fresh source shows `stale=false`.
8. `dashboard export`: writes a self-contained HTML file (no `http`/`https` external refs);
   contains the latest values.

(Socket binding is avoided in the suite — `route`, `Broadcaster`, and `DbWatcher` are tested
directly. One optional smoke test may bind `127.0.0.1:0` guarded by pytest-socket allowances;
if the marker isn't available it is skipped, not failed.)

## Done-criteria

Full suite + ruff + mypy(strict) + coverage gate green; fresh-agent verdict PASS; no new
runtime dependency added to `pyproject.toml`; `ARCHITECTURE.md` + `RUNBOOK.md` updated with
the dashboard (routes, `dashboard serve|export`, "never calls upstream" invariant); merge
`--no-ff`, tag `s12b-complete`.
