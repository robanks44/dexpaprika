"""LP discovery — replay of the live probe's raw calls, custody rules, recording."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from dexpaprika.chains.rpc import EvmRpcClient
from dexpaprika.config import Settings
from dexpaprika.lp.discovery import discover, record
from dexpaprika.quota import QuotaTracker
from dexpaprika.storage.db import connect
from dexpaprika.storage.migrations import migrate

PROBE = json.loads(
    (Path(__file__).parent.parent / "probes" / "out" / "s5" / "discovery.json").read_text()
)
WALLET = PROBE["wallet"]
RAW = {k.lower(): v for k, v in PROBE["raw_calls"].items()}
ZERO_WORD = "0x" + "0" * 64


def replay_handler(overrides: dict[str, str] | None = None) -> httpx.MockTransport:
    """Serve the recorded probe raws; unknown calls return a zero word."""
    table = dict(RAW)
    if overrides:
        table.update({k.lower(): v for k, v in overrides.items()})

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "eth_blockNumber":
            result: Any = hex(PROBE["pin"] + 3)
        else:
            tx = body["params"][0]
            key = f"{tx['to'].lower()}|{tx['data']}"
            result = table.get(key.lower(), ZERO_WORD)
        return httpx.Response(200, text=json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}))

    return httpx.MockTransport(handle)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "t.db")
    migrate(connection)
    QuotaTracker(connection).ensure_providers()
    yield connection
    connection.close()


def make_rpc(conn: sqlite3.Connection, handler: httpx.MockTransport) -> EvmRpcClient:
    settings = Settings.load()
    clients = [
        httpx.Client(transport=handler, base_url=url, headers={"User-Agent": "dexpaprika/1.0"})
        for url in settings.base_rpc_urls
    ]
    return EvmRpcClient(conn, "base", settings=settings, clients=clients, sleeper=lambda _s: None)


def test_discovery_finds_sickle_held_position(conn: sqlite3.Connection) -> None:
    rpc = make_rpc(conn, replay_handler())
    positions = discover(rpc, WALLET, settings=Settings.load(), block=PROBE["pin"])
    assert len(positions) == 1
    p = positions[0]
    assert p.token_id == 5056427
    assert p.custody == "sickle"
    assert p.custodian.lower() == PROBE["sickle"].lower()
    assert p.tick_lower == -202000
    assert p.tick_upper == -200000
    assert p.liquidity == 3987414535131380
    assert p.pool is not None
    assert p.pool.lower() == PROBE["resolved_pool"].lower()
    assert p.pool_tick == PROBE["pool_tick"]
    assert p.in_range is True
    assert p.amount0 is not None
    assert p.amount0.quantize(Decimal("0.000001")) == Decimal(PROBE["computed"]["weth"])
    assert p.amount1 is not None
    assert p.amount1.quantize(Decimal("0.01")) == Decimal(PROBE["computed"]["usdc"])
    assert p.price_usd is not None
    assert p.price_usd.quantize(Decimal("0.01")) == Decimal(PROBE["computed"]["price_usd"])


def test_empty_wallet_returns_clean_empty(conn: sqlite3.Connection) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        result = hex(12345) if body["method"] == "eth_blockNumber" else ZERO_WORD
        return httpx.Response(200, text=json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}))

    rpc = make_rpc(conn, httpx.MockTransport(handle))
    positions = discover(rpc, WALLET, settings=Settings.load(), block=12000)
    assert positions == []


def test_sickle_owner_mismatch_excludes_sickle(conn: sqlite3.Connection) -> None:
    """Custody verification: a sickle not owned by the wallet is NOT enumerated."""
    sickle = PROBE["sickle"].lower()
    from dexpaprika.chains.abi import selector

    overrides = {
        f"{sickle}|{selector('owner()')}": "0x" + "ff" * 32  # wrong owner
    }
    rpc = make_rpc(conn, replay_handler(overrides))
    positions = discover(rpc, WALLET, settings=Settings.load(), block=PROBE["pin"])
    # The only position was sickle-held; with the sickle rejected, none remain.
    assert positions == []


def test_record_position_end_to_end(conn: sqlite3.Connection) -> None:
    rpc = make_rpc(conn, replay_handler())
    position = discover(rpc, WALLET, settings=Settings.load(), block=PROBE["pin"])[0]
    record(conn, WALLET, position, "2026-08-02T12:00:00+00:00")
    record(conn, WALLET, position, "2026-08-02T13:00:00+00:00")  # idempotent upsert

    assert conn.execute("SELECT COUNT(*) AS n FROM positions").fetchone()["n"] == 1
    row = conn.execute("SELECT * FROM positions").fetchone()
    assert row["venue"] == "aerodrome-slipstream"
    assert row["kind"] == "lp"
    assert row["group_tag"] == "lp_hedge"
    assert row["external_id"].endswith(":5056427")

    events = conn.execute("SELECT * FROM position_events ORDER BY ts").fetchall()
    assert len(events) == 2
    state = json.loads(events[0]["state_json"])
    assert state["tick_lower"] == -202000
    assert state["custody"] == "sickle"
    assert Decimal(state["amount0"]) > 0


def _sel_arg(sig: str, *args: str) -> str:
    from dexpaprika.chains.abi import selector

    return selector(sig) + "".join(args)


def _addr_word(address: str) -> str:
    return f"{int(address, 16):064x}"


def test_gauge_staked_position_discovered_and_deduped(conn: sqlite3.Connection) -> None:
    """Gauge path finds a staked id; dedup keeps one row when sickle also holds it."""
    gauge = PROBE["gauge"].lower()
    staked_one = (
        "0x"
        + f"{32:064x}"  # offset
        + f"{1:064x}"  # length
        + f"{5056427:064x}"
    )
    overrides = {
        f"{gauge}|{_sel_arg('stakedValues(address)', _addr_word(PROBE['sickle']))}": staked_one
    }
    rpc = make_rpc(conn, replay_handler(overrides))
    positions = discover(rpc, WALLET, settings=Settings.load(), block=PROBE["pin"])
    assert len(positions) == 1  # deduped: sickle enumeration already found it


def test_pool_unresolved_flags_without_valuing(conn: sqlite3.Connection) -> None:
    factory = PROBE["factory_second"].lower()
    pos = PROBE["positions"][0]
    key = f"{factory}|" + _sel_arg(
        "getPool(address,address,int24)",
        _addr_word(pos["token0"]),
        _addr_word(pos["token1"]),
        f"{pos['tick_spacing']:064x}",
    )
    rpc = make_rpc(conn, replay_handler({key: ZERO_WORD}))
    positions = discover(rpc, WALLET, settings=Settings.load(), block=PROBE["pin"])
    p = positions[0]
    assert p.pool_unresolved is True
    assert p.amount0 is None
    assert any("unresolved" in w for w in p.warnings)


def test_unknown_token_decimals_records_raw_only(conn: sqlite3.Connection) -> None:
    """A pair outside the decimals registry is recorded but never mis-valued."""
    nfpm = PROBE["positions"][0]["nfpm"].lower()
    pos_key = f"{nfpm}|" + _sel_arg("positions(uint256)", f"{5056427:064x}")
    original = RAW[pos_key]
    # Swap token0 for an unknown token address (word 2).
    words = [original[2:][i : i + 64] for i in range(0, len(original) - 2, 64)]
    words[2] = f"{int('0x' + 'ab' * 20, 16):064x}"
    patched = "0x" + "".join(words)
    # getPool for the new pair still resolves to the known pool.
    factory = PROBE["factory_second"].lower()
    pool_key = f"{factory}|" + _sel_arg(
        "getPool(address,address,int24)",
        words[2][-64:],
        _addr_word(PROBE["positions"][0]["token1"]),
        f"{PROBE['positions'][0]['tick_spacing']:064x}",
    )
    pool_word = f"{int(PROBE['resolved_pool'], 16):064x}"
    rpc = make_rpc(conn, replay_handler({pos_key: patched, pool_key: "0x" + pool_word}))
    positions = discover(rpc, WALLET, settings=Settings.load(), block=PROBE["pin"])
    p = positions[0]
    assert p.amount0 is None
    assert any("decimals" in w for w in p.warnings)
