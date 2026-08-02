"""GMX client — exact scaling (the silent-wrong-alert trap), failover, recording."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from dexpaprika.clients.base import TransportError
from dexpaprika.clients.gmx import (
    UINT256_MAX,
    GmxClient,
    scale_factor,
    scale_tokens,
    scale_trigger,
    scale_usd,
)
from dexpaprika.config import Settings
from dexpaprika.quota import QuotaTracker
from dexpaprika.storage.db import connect
from dexpaprika.storage.migrations import migrate

FIXTURES = Path(__file__).parent.parent / "probes" / "out" / "s4"
WALLET = "0xC155A616e39D7B83E37e8FD9d2106E1BC056d7Fe"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "t.db")
    migrate(connection)
    QuotaTracker(connection).ensure_providers()
    yield connection
    connection.close()


def make_client(
    conn: sqlite3.Connection,
    handlers: list[httpx.MockTransport],
) -> GmxClient:
    """Client whose peers are served by the given mock transports, in order."""
    settings = Settings.load()
    clients = [
        httpx.Client(transport=handler, base_url=peer)
        for handler, peer in zip(handlers, settings.gmx_rest_peers, strict=False)
    ]
    return GmxClient(conn, settings=settings, clients=clients, sleeper=lambda _s: None)


def fixture_handler(name: str) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/positions"):
            return httpx.Response(200, text=(FIXTURES / name).read_text())
        if path.endswith("/markets"):
            return httpx.Response(200, text=(FIXTURES / "markets.json").read_text())
        if path.endswith("/orders"):
            return httpx.Response(200, text=(FIXTURES / "orders.json").read_text())
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handle)


class TestScaling:
    """VERIFIED_FINDINGS §2.1 — exact Decimal, pinned to live-verified numbers."""

    def test_usd_1e30_exact(self) -> None:
        assert scale_usd("13155762269646219571243906932000000") == Decimal(
            "13155.762269646219571243906932"
        )

    def test_leverage_1e4(self) -> None:
        assert scale_factor("19694") == Decimal("1.9694")

    def test_tokens_native_decimals(self) -> None:
        assert scale_tokens("7038573460810147061", 18) == Decimal("7.038573460810147061")
        assert scale_tokens("6579725157", 6) == Decimal("6579.725157")

    def test_trigger_price_1e12_for_eth(self) -> None:
        # THE trap: 1e12 for ETH (10^(30-18)), NOT 1e30.
        assert scale_trigger("1925000000000000", 18) == Decimal("1925")

    def test_trigger_wrong_scale_would_be_absurd(self) -> None:
        # Guard documentation: applying 1e30 to a trigger yields a sub-cent
        # number — the silent-wrong-alert failure this table prevents.
        assert scale_usd("1925000000000000") < Decimal("0.01")

    @given(raw=st.integers(min_value=0, max_value=10**40))
    def test_usd_scaling_never_loses_precision(self, raw: int) -> None:
        scaled = scale_usd(str(raw))
        assert scaled * Decimal(10) ** 30 == Decimal(raw)

    @given(raw=st.integers(min_value=0, max_value=10**30), decimals=st.sampled_from([6, 8, 18]))
    def test_token_scaling_round_trips(self, raw: int, decimals: int) -> None:
        scaled = scale_tokens(str(raw), decimals)
        assert scaled * Decimal(10) ** decimals == Decimal(raw)


class TestParsing:
    def test_position_parsed_to_verified_numbers(self, conn: sqlite3.Connection) -> None:
        client = make_client(conn, [fixture_handler("positions_peer_io.json")])
        positions = client.get_positions(WALLET)
        assert len(positions) == 1
        p = positions[0]
        assert p.index_name == "ETH/USD"
        assert p.is_long is False
        assert p.size_usd == Decimal("13155.762269646219571243906932")
        assert p.size_tokens == Decimal("7.038573460810147061")
        assert p.entry_price == Decimal("1869.094972567349999993975016")
        assert p.liquidation_price is not None
        assert p.leverage == Decimal("1.9694")
        # entryPrice ≡ sizeInUsd / sizeInTokens (verified invariant).
        derived = p.size_usd / p.size_tokens
        assert abs(derived - p.entry_price) < Decimal("0.000001")

    def test_stop_loss_order_parsed(self, conn: sqlite3.Connection) -> None:
        client = make_client(conn, [fixture_handler("positions_peer_io.json")])
        p = client.get_positions(WALLET)[0]
        assert len(p.related_orders) == 1
        order = p.related_orders[0]
        assert order.order_type == 6
        assert order.order_kind == "stop-loss-decrease"
        assert order.trigger_price == Decimal("1925")
        assert order.is_full_close is True
        assert order.auto_cancel is True

    def test_empty_positions_is_valid_no_position(self, conn: sqlite3.Connection) -> None:
        handler = httpx.MockTransport(lambda _r: httpx.Response(200, text="[]"))
        client = make_client(conn, [handler])
        assert client.get_positions(WALLET) == []

    def test_unknown_order_type_defensive(self, conn: sqlite3.Connection) -> None:
        raw = (
            (FIXTURES / "positions_peer_io.json")
            .read_text()
            .replace('"orderType":6', '"orderType":99')
            .replace('"orderType": 6', '"orderType": 99')
        )
        handler = httpx.MockTransport(lambda _r: httpx.Response(200, text=raw))
        client = make_client(conn, [handler])
        order = client.get_positions(WALLET)[0].related_orders[0]
        assert order.order_kind == "unknown-99"

    def test_hedge_positions_filter(self, conn: sqlite3.Connection) -> None:
        client = make_client(conn, [fixture_handler("positions_peer_io.json")])
        hedge = client.hedge_positions(WALLET)
        assert len(hedge) == 1
        assert hedge[0].is_long is False

    def test_markets_parsed_by_symbol_not_indexname(self, conn: sqlite3.Connection) -> None:
        """Probe catch: /markets carries symbol, not indexName."""
        client = make_client(conn, [fixture_handler("positions_peer_io.json")])
        markets = client.get_markets()
        ours = [m for m in markets if m.market_token_address.lower().startswith("0x70d95587")]
        assert ours[0].symbol.startswith("ETH/USD")
        assert ours[0].index_token_address.lower().startswith("0x82af4944")


class TestFailover:
    def test_second_peer_used_when_first_fails(self, conn: sqlite3.Connection) -> None:
        dead = httpx.MockTransport(lambda _r: httpx.Response(500, text="down"))
        client = make_client(conn, [dead, fixture_handler("positions_peer_ai.json")])
        positions = client.get_positions(WALLET)
        assert len(positions) == 1

    def test_all_peers_dead_names_both(self, conn: sqlite3.Connection) -> None:
        dead = httpx.MockTransport(lambda _r: httpx.Response(500, text="down"))
        client = make_client(conn, [dead, dead])
        with pytest.raises(TransportError, match=r"gmxapi\.io.*gmxapi\.ai|all.*peers"):
            client.get_positions(WALLET)


class TestRecording:
    def test_observation_recorded_end_to_end(self, conn: sqlite3.Connection) -> None:
        client = make_client(conn, [fixture_handler("positions_peer_io.json")])
        position = client.get_positions(WALLET)[0]
        client.record_observation(position)

        pos_row = conn.execute("SELECT * FROM positions").fetchone()
        assert pos_row["venue"] == "gmx"
        assert pos_row["chain"] == "arbitrum"
        assert pos_row["kind"] == "perp"
        assert pos_row["group_tag"] == "lp_hedge"

        event = conn.execute("SELECT * FROM position_events").fetchone()
        assert event["type"] == "observed"
        assert "1925" in event["state_json"]  # SL trigger visible in state

        order_row = conn.execute("SELECT * FROM orders").fetchone()
        assert order_row["order_type"] == 6
        assert Decimal(order_row["trigger_price"]) == Decimal("1925")
        assert order_row["size_delta"] == "FULL_CLOSE"

    def test_re_recording_is_idempotent_on_position(self, conn: sqlite3.Connection) -> None:
        client = make_client(conn, [fixture_handler("positions_peer_io.json")])
        position = client.get_positions(WALLET)[0]
        client.record_observation(position)
        client.record_observation(position)
        assert conn.execute("SELECT COUNT(*) AS n FROM positions").fetchone()["n"] == 1
        # Observations append (time series); position row does not duplicate.
        assert conn.execute("SELECT COUNT(*) AS n FROM position_events").fetchone()["n"] == 2


def test_uint256_max_constant() -> None:
    assert UINT256_MAX == 2**256 - 1
