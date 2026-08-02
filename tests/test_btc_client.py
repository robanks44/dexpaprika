"""BTC holdings client (S5.5) — probe fixtures, exact sats math, failover."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from dexpaprika.clients.base import TransportError
from dexpaprika.clients.btc import BtcClient
from dexpaprika.config import Settings
from dexpaprika.storage.db import connect
from dexpaprika.storage.migrations import migrate

PROBE = json.loads(
    (Path(__file__).parent.parent / "probes" / "out" / "s55" / "address_stats.json").read_text()
)
ADDRESS = PROBE["address"]
PAYLOAD = json.dumps(PROBE["blockstream"]["payload"])


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "t.db")
    migrate(connection)
    from dexpaprika.quota import QuotaTracker

    QuotaTracker(connection).ensure_providers()
    yield connection
    connection.close()


def _client(
    conn: sqlite3.Connection,
    blockstream: httpx.MockTransport | None = None,
    mempool: httpx.MockTransport | None = None,
) -> BtcClient:
    ok = httpx.MockTransport(lambda _r: httpx.Response(200, text=PAYLOAD))
    settings = Settings.load()
    peers = settings.btc_esplora_peers
    clients = [
        httpx.Client(transport=blockstream or ok, base_url=peers[0]),
        httpx.Client(transport=mempool or ok, base_url=peers[1]),
    ]
    return BtcClient(conn, settings=settings, clients=clients, sleeper=lambda _s: None)


class TestStats:
    def test_probe_fixture_exact_decimal(self, conn: sqlite3.Connection) -> None:
        stats = _client(conn).get_address(ADDRESS)
        assert stats.confirmed_sats == 131828
        assert stats.pending_sats == 131828  # zero mempool delta
        assert stats.tx_count == 2
        assert str(stats.balance_btc) == "0.00131828"

    def test_balance_is_funded_minus_spent_plus_mempool_delta(
        self, conn: sqlite3.Connection
    ) -> None:
        payload = json.dumps(
            {
                "address": ADDRESS,
                "chain_stats": {
                    "funded_txo_count": 3,
                    "funded_txo_sum": 500000,
                    "spent_txo_count": 1,
                    "spent_txo_sum": 200000,
                    "tx_count": 4,
                },
                "mempool_stats": {
                    "funded_txo_count": 1,
                    "funded_txo_sum": 50000,
                    "spent_txo_count": 1,
                    "spent_txo_sum": 10000,
                    "tx_count": 2,
                },
            }
        )
        transport = httpx.MockTransport(lambda _r: httpx.Response(200, text=payload))
        stats = _client(conn, blockstream=transport).get_address(ADDRESS)
        assert stats.confirmed_sats == 300000
        assert stats.pending_sats == 340000
        assert stats.unconfirmed_tx_count == 2
        assert stats.balance_btc == Decimal("0.003")


class TestFailover:
    def test_blockstream_down_mempool_serves(self, conn: sqlite3.Connection) -> None:
        dead = httpx.MockTransport(lambda _r: httpx.Response(503, text="down"))
        stats = _client(conn, blockstream=dead).get_address(ADDRESS)
        assert stats.confirmed_sats == 131828
        assert "mempool" in stats.source

    def test_all_peers_dead_raises_with_detail(self, conn: sqlite3.Connection) -> None:
        dead = httpx.MockTransport(lambda _r: httpx.Response(503, text="down"))
        with pytest.raises(TransportError) as excinfo:
            _client(conn, blockstream=dead, mempool=dead).get_address(ADDRESS)
        assert "peer" in str(excinfo.value).lower()


class TestRecord:
    def test_record_idempotent_upsert_two_observations(self, conn: sqlite3.Connection) -> None:
        client = _client(conn)
        stats = client.get_address(ADDRESS)
        client.record(ADDRESS, stats, "2026-08-02T10:00:00+00:00")
        client.record(ADDRESS, stats, "2026-08-02T11:00:00+00:00")
        positions = conn.execute(
            "SELECT * FROM positions WHERE chain='bitcoin' AND kind='holding'"
        ).fetchall()
        assert len(positions) == 1
        assert positions[0]["group_tag"] == "holdings"
        assert positions[0]["venue"] == "native"
        events = conn.execute(
            "SELECT state_json FROM position_events WHERE position_id=?",
            (positions[0]["id"],),
        ).fetchall()
        assert len(events) == 2
        state = json.loads(events[0]["state_json"])
        assert state["symbol"] == "BTC"
        assert state["amount"] == "0.00131828"
        assert state["confirmed_sats"] == 131828
        assert state["source"]
