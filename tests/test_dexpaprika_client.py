"""DexPaprika client — probe-fixture parsing, recording, role boundary."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from dexpaprika.clients.dexpaprika import DexPaprikaClient
from dexpaprika.quota import QuotaTracker
from dexpaprika.storage.db import connect
from dexpaprika.storage.migrations import migrate

FIXTURES = Path(__file__).parent.parent / "probes" / "out" / "s3"
POOL = "0x56aeaf4af2df4bdfd9d865830fefdd278b25e7ef"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "t.db")
    migrate(connection)
    QuotaTracker(connection).ensure_providers()
    yield connection
    connection.close()


def make_client(conn: sqlite3.Connection, routes: dict[str, str]) -> DexPaprikaClient:
    """Client with a mock transport serving recorded probe payloads."""

    def handle(request: httpx.Request) -> httpx.Response:
        for suffix, body in routes.items():
            if request.url.path.endswith(suffix.split("?")[0]):
                return httpx.Response(200, text=body)
        return httpx.Response(404, text="not found")

    http_client = httpx.Client(
        transport=httpx.MockTransport(handle), base_url="https://api.dexpaprika.com"
    )
    return DexPaprikaClient(conn=conn, client=http_client, sleeper=lambda _s: None)


def test_get_networks_from_probe_fixture(conn: sqlite3.Connection) -> None:
    client = make_client(conn, {"/networks": (FIXTURES / "networks.json").read_text()})
    networks = client.get_networks()
    ids = {n.id for n in networks}
    assert {"base", "arbitrum"} <= ids


def test_get_pool_parses_probe_fixture_with_decimals(conn: sqlite3.Connection) -> None:
    client = make_client(conn, {f"/pools/{POOL}": (FIXTURES / "pool_details.json").read_text()})
    pool = client.get_pool("base", POOL)
    assert pool.dex_id == "aerodrome_slipstream_2"
    assert isinstance(pool.last_price_usd, Decimal)
    assert pool.fee is None  # SlipStream: fee tier NOT available here (probe-verified)
    assert pool.volume_24h_usd is not None
    assert isinstance(pool.volume_24h_usd, Decimal)


def test_get_ohlcv_parses_probe_fixture(conn: sqlite3.Connection) -> None:
    client = make_client(conn, {"/ohlcv": (FIXTURES / "ohlcv_24h.json").read_text()})
    candles = client.get_ohlcv("base", POOL, start="2026-07-26", interval="24h", limit=7)
    assert len(candles) >= 5
    first = candles[0]
    assert first.time_open.startswith("2026-07-26")
    assert isinstance(first.open, Decimal)
    assert isinstance(first.close, Decimal)
    assert first.high >= first.low


def test_invalid_interval_rejected(conn: sqlite3.Connection) -> None:
    client = make_client(conn, {})
    with pytest.raises(ValueError, match="interval"):
        client.get_ohlcv("base", POOL, start="2026-07-26", interval="7h")


def test_record_pool_metrics_carries_source_and_as_of(conn: sqlite3.Connection) -> None:
    client = make_client(conn, {f"/pools/{POOL}": (FIXTURES / "pool_details.json").read_text()})
    pool = client.get_pool("base", POOL)
    client.record_pool_metrics(pool)
    row = conn.execute("SELECT * FROM pool_metrics").fetchone()
    assert row["source"] == "dexpaprika"
    assert row["network"] == "base"
    assert row["pool_address"] == POOL
    assert row["ts"]  # as_of recorded
    assert Decimal(row["price_usd"]) == pool.last_price_usd


def test_record_ohlcv_idempotent_upsert(conn: sqlite3.Connection) -> None:
    client = make_client(conn, {"/ohlcv": (FIXTURES / "ohlcv_24h.json").read_text()})
    candles = client.get_ohlcv("base", POOL, start="2026-07-26", interval="24h", limit=7)
    first_count = client.record_ohlcv("base", POOL, "24h", candles)
    second_count = client.record_ohlcv("base", POOL, "24h", candles)
    rows = conn.execute("SELECT COUNT(*) AS n FROM ohlcv").fetchone()["n"]
    assert first_count == len(candles)
    assert rows == len(candles)  # re-record did not duplicate
    assert second_count == len(candles)


def test_client_exposes_no_hedge_price_api(conn: sqlite3.Connection) -> None:
    """Role boundary (VERIFIED_FINDINGS §3): no 'current price for hedge math' API."""
    client = make_client(conn, {})
    forbidden = [n for n in dir(client) if "hedge" in n.lower() or n == "get_price"]
    assert forbidden == []


def test_raw_payload_retained_for_audit(conn: sqlite3.Connection) -> None:
    client = make_client(conn, {f"/pools/{POOL}": (FIXTURES / "pool_details.json").read_text()})
    pool = client.get_pool("base", POOL)
    client.record_pool_metrics(pool)
    raw = conn.execute("SELECT raw_json FROM pool_metrics").fetchone()["raw_json"]
    assert json.loads(raw)["dex_id"] == "aerodrome_slipstream_2"
