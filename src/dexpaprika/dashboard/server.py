"""Stdlib http.server adapter for the dashboard (S12b).

Thin glue: non-SSE paths go through ``app.route``; ``/events`` streams SSE from a
``Broadcaster`` fed by a ``DbWatcher`` that watches the LOCAL DB for new snapshot
rows (never an upstream poll). ``Broadcaster`` and ``DbWatcher`` are socket-free
so they are unit-tested directly; only the handler binds a socket.
"""

from __future__ import annotations

import gzip
import queue
import sqlite3
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from dexpaprika.config import Settings
from dexpaprika.dashboard import app

ConnFactory = Callable[[], sqlite3.Connection]


class Broadcaster:
    """Thread-safe fan-out of string events to all SSE subscribers."""

    def __init__(self) -> None:
        self._subs: set[queue.Queue[str]] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue(maxsize=16)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue[str]) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: str) -> int:
        with self._lock:
            subs = list(self._subs)
        delivered = 0
        for q in subs:
            try:
                q.put_nowait(event)
                delivered += 1
            except queue.Full:
                pass  # a slow client drops this tick, never blocks the writer
        return delivered

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


class DbWatcher:
    """Publish an ``update`` whenever the local snapshots table gains a row.

    A LOCAL DB watch — reads ``MAX(id) FROM snapshots`` on its own connection and
    publishes on change. Never contacts upstream. ``sleep``/``stop`` are injected
    so tests advance it deterministically with zero real waiting.
    """

    def __init__(
        self,
        conn_factory: ConnFactory,
        broadcaster: Broadcaster,
        *,
        sleep: Callable[[float], None],
        stop: Callable[[], bool],
        interval: float = 1.0,
    ) -> None:
        self._conn_factory = conn_factory
        self._broadcaster = broadcaster
        self._sleep = sleep
        self._stop = stop
        self._interval = interval
        probe = conn_factory()
        try:
            self._last = self._max_id(probe)
        finally:
            probe.close()

    @staticmethod
    def _max_id(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM snapshots").fetchone()
        try:
            return int(row["m"])
        except (TypeError, KeyError):
            return int(row[0])

    def run(self, *, max_ticks: int | None = None) -> int:
        """Loop until stop/max_ticks; return the number of `update`s published."""
        conn = self._conn_factory()
        published = 0
        ticks = 0
        try:
            while not self._stop() and (max_ticks is None or ticks < max_ticks):
                current = self._max_id(conn)
                if current > self._last:
                    self._broadcaster.publish("update")
                    self._last = current
                    published += 1
                ticks += 1
                if not self._stop() and (max_ticks is None or ticks < max_ticks):
                    self._sleep(self._interval)
        finally:
            conn.close()
        return published


class _Handler(BaseHTTPRequestHandler):
    server_version = "dexpaprika-dashboard/1"

    # injected on the server instance
    conn_factory: ConnFactory
    settings: Settings
    broadcaster: Broadcaster

    def log_message(self, *_args: Any) -> None:  # silence default stderr spam
        pass

    def _srv(self) -> Any:
        return self.server

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/events":
            self._serve_sse()
            return
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        srv = self._srv()
        conn = srv.conn_factory()
        try:
            result = app.route(parsed.path, query, conn, srv.settings)
        finally:
            conn.close()
        body = result.body
        headers = dict(result.headers)
        # Honour Accept-Encoding: only ship gzip if the client accepts it;
        # otherwise decompress so a non-browser client still gets valid bytes.
        if headers.get("Content-Encoding") == "gzip":
            accepts = "gzip" in self.headers.get("Accept-Encoding", "")
            if not accepts:
                body = gzip.decompress(body)
                del headers["Content-Encoding"]
        self.send_response(result.status)
        self.send_header("Content-Type", result.content_type)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_sse(self) -> None:
        srv = self._srv()
        sub = srv.broadcaster.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = sub.get(timeout=15.0)
                    self.wfile.write(f"event: {event}\ndata: {{}}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")  # keep the connection warm
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            pass
        finally:
            srv.broadcaster.unsubscribe(sub)


def serve(
    conn_factory: ConnFactory,
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    broadcaster: Broadcaster | None = None,
) -> ThreadingHTTPServer:
    """Build (not block on) the dashboard server. Caller runs serve_forever()."""
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.conn_factory = conn_factory  # type: ignore[attr-defined]
    httpd.settings = settings  # type: ignore[attr-defined]
    httpd.broadcaster = broadcaster or Broadcaster()  # type: ignore[attr-defined]
    return httpd
