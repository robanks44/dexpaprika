"""EVM read layer — ABI helpers pinned to live fixtures, pin tripwire, failover."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from dexpaprika.chains.abi import (
    ARBSYS,
    MULTICALL3,
    decode_aggregate,
    decode_uint,
    encode_call_aggregate,
    encode_uint,
    selector,
    sign_extend,
)
from dexpaprika.chains.rpc import ChainRpcError, EvmRpcClient, PinMismatchError
from dexpaprika.config import Settings
from dexpaprika.quota import QuotaTracker
from dexpaprika.storage.db import connect
from dexpaprika.storage.migrations import migrate

PROBE = json.loads(
    (Path(__file__).parent.parent / "probes" / "out" / "s45" / "pinned_multicall.json").read_text()
)
POOL = "0x56aeaf4af2df4bdfd9d865830fefdd278b25e7ef"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "t.db")
    migrate(connection)
    QuotaTracker(connection).ensure_providers()
    yield connection
    connection.close()


class TestAbi:
    def test_selectors_match_verified_values(self) -> None:
        assert selector("slot0()") == "0x3850c7bd"
        assert selector("liquidity()") == "0x1a686502"
        assert selector("tickSpacing()") == "0xd0c93a7c"
        assert selector("positions(uint256)") == "0x99fbab88"
        assert selector("arbBlockNumber()") == "0xa3b1b31d"  # probe-pinned
        assert selector("aggregate((address,bytes)[])") == "0x252dba42"
        assert selector("getBlockNumber()") == "0x42cbb15c"

    def test_aggregate_encoding_matches_live_probe_calldata(self) -> None:
        """Byte-for-byte against the calldata that executed on Base (probe)."""
        calls = [
            (MULTICALL3, "0x42cbb15c"),
            (MULTICALL3, "0x3408e470"),
            (POOL, "0x3850c7bd"),
            (POOL, "0x1a686502"),
        ]
        assert encode_call_aggregate(calls) == PROBE["base"]["calldata"]

    def test_aggregate_decoding_matches_live_probe_response(self) -> None:
        block_number, outs = decode_aggregate(PROBE["base"]["raw_response"])
        assert block_number == PROBE["base"]["aggregate_block"]
        assert decode_uint(outs[0]) == PROBE["base"]["inner_getBlockNumber"]
        assert decode_uint(outs[1]) == 8453
        tick = sign_extend(int.from_bytes(outs[2][32:64], "big"), 256)
        assert tick == PROBE["base"]["pool_tick"]  # -201118, sign-extended
        assert decode_uint(outs[3]) == PROBE["base"]["pool_liquidity"]

    def test_sign_extend_known_values(self) -> None:
        assert sign_extend((1 << 256) - 201118, 256) == -201118
        assert sign_extend(0xFFFFFF, 24) == -1
        assert sign_extend(0x7FFFFF, 24) == 0x7FFFFF
        assert sign_extend(5, 24) == 5

    @given(value=st.integers(min_value=-(2**23), max_value=2**23 - 1))
    def test_sign_extend_int24_round_trip(self, value: int) -> None:
        word = value % (1 << 24)
        assert sign_extend(word, 24) == value

    def test_encode_uint_padding(self) -> None:
        assert encode_uint(5056427) == f"{5056427:064x}"
        assert len(encode_uint(0)) == 64


def rpc_handler(
    dispatch: dict[str, Callable[[list[Any]], Any]],
) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body["method"]
        if method not in dispatch:
            return httpx.Response(
                200,
                text=json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "no method"}}
                ),
            )
        result = dispatch[method](body["params"])
        return httpx.Response(200, text=json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}))

    return httpx.MockTransport(handle)


def make_client(
    conn: sqlite3.Connection,
    chain: str,
    handlers: list[httpx.MockTransport],
) -> EvmRpcClient:
    settings = Settings.load()
    urls = settings.base_rpc_urls if chain == "base" else settings.arbitrum_rpc_urls
    clients = [
        httpx.Client(transport=h, base_url=u, headers={"User-Agent": "dexpaprika/1.0"})
        for h, u in zip(handlers, urls, strict=False)
    ]
    return EvmRpcClient(conn, chain, settings=settings, clients=clients, sleeper=lambda _s: None)


def base_dispatch(pin_offset: int = 0) -> dict[str, Callable[[list[Any]], Any]]:
    """A healthy Base node replaying the probe aggregate response."""

    def eth_call(params: list[Any]) -> str:
        raw: str = PROBE["base"]["raw_response"]
        if pin_offset:
            block_number, _outs = decode_aggregate(raw)
            b = bytes.fromhex(raw[2:])
            patched = (block_number + pin_offset).to_bytes(32, "big") + b[32:]
            return "0x" + patched.hex()
        return raw

    return {
        "eth_blockNumber": lambda _p: hex(PROBE["base"]["pin"] + 3),
        "eth_call": eth_call,
    }


class TestRpcClient:
    def test_block_number_and_pin(self, conn: sqlite3.Connection) -> None:
        client = make_client(conn, "base", [rpc_handler(base_dispatch())])
        assert client.block_number() == PROBE["base"]["pin"] + 3
        assert client.resolve_pin(margin=3) == PROBE["base"]["pin"]

    def test_snapshot_verifies_tripwire_and_records(self, conn: sqlite3.Connection) -> None:
        client = make_client(conn, "base", [rpc_handler(base_dispatch())])
        snap = client.snapshot("test", extra_calls=[(POOL, "0x3850c7bd"), (POOL, "0x1a686502")])
        assert snap.block_number == PROBE["base"]["pin"]
        assert len(snap.results) == 2  # tripwire + chainId consumed internally
        row = conn.execute("SELECT * FROM snapshots").fetchone()
        assert row["chain"] == "base"
        assert row["block_number"] == PROBE["base"]["pin"]
        assert row["kind"] == "test"

    def test_pin_mismatch_raises_and_does_not_record(self, conn: sqlite3.Connection) -> None:
        client = make_client(conn, "base", [rpc_handler(base_dispatch(pin_offset=-5))])
        with pytest.raises(PinMismatchError, match="lagging|pinned"):
            client.snapshot("test")
        assert conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"] == 0

    def test_ring_failover(self, conn: sqlite3.Connection) -> None:
        dead = httpx.MockTransport(lambda _r: httpx.Response(500, text="down"))
        client = make_client(conn, "base", [dead, rpc_handler(base_dispatch())])
        assert client.block_number() == PROBE["base"]["pin"] + 3

    def test_revert_fails_fast_with_clear_error(self, conn: sqlite3.Connection) -> None:
        def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "error": {"code": 3, "message": "execution reverted"},
                    }
                ),
            )

        client = make_client(conn, "base", [httpx.MockTransport(handle)])
        with pytest.raises(ChainRpcError, match="revert"):
            client.call(POOL, "0x3850c7bd", "latest")

    def test_calls_are_quota_logged_per_chain_ring(self, conn: sqlite3.Connection) -> None:
        client = make_client(conn, "base", [rpc_handler(base_dispatch())])
        client.block_number()
        row = conn.execute(
            "SELECT p.name FROM api_call_log l JOIN providers p ON p.id = l.provider_id"
        ).fetchone()
        assert row["name"] == "base-rpc"


class TestArbitrumTripwire:
    """Probe catch: Multicall3 block.number is the L1 block on Arbitrum."""

    def test_arbitrum_uses_arbsys_tripwire(self, conn: sqlite3.Connection) -> None:
        pin = PROBE["arbitrum"]["arbsys_pin_used"]
        seen_calls: list[str] = []

        def eth_call(params: list[Any]) -> str:
            calldata = params[0]["data"]
            seen_calls.append(calldata)
            # Decode which inner calls were batched; respond accordingly.
            # aggregate returns L1-ish block outer, but ArbSys inner returns pin.
            outer_l1 = 25664170
            inner = [
                encode_uint(pin),  # ArbSys.arbBlockNumber → L2 pin
                encode_uint(42161),  # chainId
            ]
            arr_off = 64
            head = f"{outer_l1:064x}" + f"{arr_off:064x}".rjust(64, "0")
            n = len(inner)
            offsets = []
            bodies = []
            running = 32 * n
            for word in inner:
                offsets.append(f"{running:064x}")
                bodies.append(f"{32:064x}" + word)
                running += 64
            return "0x" + head + f"{n:064x}" + "".join(offsets) + "".join(bodies)

        client = make_client(
            conn,
            "arbitrum",
            [rpc_handler({"eth_blockNumber": lambda _p: hex(pin + 3), "eth_call": eth_call})],
        )
        snap = client.snapshot("test")
        assert snap.block_number == pin
        # The batch must contain the ArbSys tripwire target, not rely on
        # Multicall3's own (L1) block number.
        assert ARBSYS[2:].lower() in seen_calls[0].lower()
