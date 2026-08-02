"""Shared HTTP transport — quota integration, Decimal parsing, retry, breaker."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from dexpaprika.clients.base import (
    CircuitOpenError,
    HttpTransport,
    TransportError,
)
from dexpaprika.quota import QuotaTracker
from dexpaprika.storage.db import connect
from dexpaprika.storage.migrations import migrate

START = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "t.db")
    migrate(connection)
    QuotaTracker(connection).ensure_providers()
    yield connection
    connection.close()


def make_transport(
    conn: sqlite3.Connection,
    handler: httpx.MockTransport,
    clock: FakeClock | None = None,
    provider: str = "dexpaprika",
    base_url: str = "https://api.dexpaprika.com",
) -> tuple[HttpTransport, FakeClock, list[float]]:
    clk = clock or FakeClock()
    slept: list[float] = []

    def sleeper(seconds: float) -> None:
        slept.append(seconds)
        clk.advance(seconds)

    transport = HttpTransport(
        base_url=base_url,
        provider=provider,
        conn=conn,
        client=httpx.Client(transport=handler, base_url=base_url),
        clock=clk,
        sleeper=sleeper,
    )
    return transport, clk, slept


def test_https_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(TransportError, match="HTTPS"):
        HttpTransport(
            base_url="http://insecure.example.com",
            provider="dexpaprika",
            conn=conn,
            client=httpx.Client(),
            clock=FakeClock(),
            sleeper=lambda _s: None,
        )


def test_json_numbers_become_decimal(conn: sqlite3.Connection) -> None:
    handler = httpx.MockTransport(
        lambda _req: httpx.Response(200, text='{"price": 1825.4110569482561}')
    )
    transport, _clk, _slept = make_transport(conn, handler)
    payload = transport.get_json("/x")
    assert isinstance(payload, dict)
    assert payload["price"] == Decimal("1825.4110569482561")
    assert isinstance(payload["price"], Decimal)


def test_calls_are_quota_checked_and_logged(conn: sqlite3.Connection) -> None:
    handler = httpx.MockTransport(lambda _req: httpx.Response(200, text="{}"))
    transport, _clk, _slept = make_transport(conn, handler)
    transport.get_json("/networks")
    row = conn.execute(
        "SELECT p.name, l.endpoint, l.status FROM api_call_log l"
        " JOIN providers p ON p.id = l.provider_id"
    ).fetchone()
    assert row["name"] == "dexpaprika"
    assert "/networks" in row["endpoint"]
    assert row["status"] == 200


def test_rate_limit_waits_before_calling(conn: sqlite3.Connection) -> None:
    calls = 0

    def handle(_req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="{}")

    clock = FakeClock()
    transport, _clk, slept = make_transport(conn, httpx.MockTransport(handle), clock)
    for _ in range(31):  # dexpaprika: 30/min
        transport.get_json("/networks")
    assert calls == 31
    assert slept  # the 31st call had to wait for a slot


def test_retry_on_5xx_then_success(conn: sqlite3.Connection) -> None:
    attempts = 0

    def handle(_req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(500, text="oops")
        return httpx.Response(200, text='{"ok": true}')

    transport, _clk, _slept = make_transport(conn, httpx.MockTransport(handle))
    payload = transport.get_json("/flaky")
    assert isinstance(payload, dict)
    assert payload["ok"] is True
    assert attempts == 3


def test_404_fails_fast_without_retry(conn: sqlite3.Connection) -> None:
    attempts = 0

    def handle(_req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404, text="not found")

    transport, _clk, _slept = make_transport(conn, httpx.MockTransport(handle))
    with pytest.raises(TransportError, match="404"):
        transport.get_json("/missing")
    assert attempts == 1


def test_persistent_5xx_exhausts_retries(conn: sqlite3.Connection) -> None:
    def handle(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    transport, _clk, _slept = make_transport(conn, httpx.MockTransport(handle))
    with pytest.raises(TransportError, match="500"):
        transport.get_json("/down")


def test_oversized_response_refused(conn: sqlite3.Connection) -> None:
    big = '{"data": "' + "x" * (10 * 1024 * 1024) + '"}'
    handler = httpx.MockTransport(lambda _req: httpx.Response(200, text=big))
    transport, _clk, _slept = make_transport(conn, handler)
    with pytest.raises(TransportError, match=r"10\s?MB|too large"):
        transport.get_json("/big")


def test_non_json_response_is_clear_error(conn: sqlite3.Connection) -> None:
    handler = httpx.MockTransport(lambda _req: httpx.Response(200, text="<html>hi</html>"))
    transport, _clk, _slept = make_transport(conn, handler)
    with pytest.raises(TransportError, match="JSON"):
        transport.get_json("/html")


class TestCircuitBreaker:
    def test_opens_after_consecutive_failures_and_recovers(self, conn: sqlite3.Connection) -> None:
        healthy = False

        def handle(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="{}") if healthy else httpx.Response(500, text="x")

        clock = FakeClock()
        transport, clk, _slept = make_transport(conn, httpx.MockTransport(handle), clock)
        for _ in range(5):
            with pytest.raises(TransportError):
                transport.get_json("/e")
        # Breaker now open: fails immediately without hitting the wire.
        with pytest.raises(CircuitOpenError, match="circuit"):
            transport.get_json("/e")
        # After cooldown, a healthy upstream closes it again.
        healthy = True
        clk.advance(61)
        payload = transport.get_json("/e")
        assert payload == {}


def test_connection_errors_retry_then_succeed(conn: sqlite3.Connection) -> None:
    attempts = 0

    def handle(_req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            msg = "connection refused"
            raise httpx.ConnectError(msg)
        return httpx.Response(200, text='{"ok": true}')

    transport, _clk, _slept = make_transport(conn, httpx.MockTransport(handle))
    payload = transport.get_json("/net")
    assert isinstance(payload, dict)
    assert payload["ok"] is True
    assert attempts == 2
    # The failed attempt was still logged (status NULL).
    rows = conn.execute("SELECT status FROM api_call_log ORDER BY id").fetchall()
    assert rows[0]["status"] is None
    assert rows[-1]["status"] == 200
