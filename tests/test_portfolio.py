"""Portfolio layer — Aave/holdings from probe fixtures, lifecycle event derivation."""

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
from dexpaprika.portfolio.aave import read_account
from dexpaprika.portfolio.aave import record as record_aave
from dexpaprika.portfolio.holdings import read_holdings
from dexpaprika.portfolio.holdings import record as record_holdings
from dexpaprika.portfolio.lifecycle import observe, reconcile_closures
from dexpaprika.quota import QuotaTracker
from dexpaprika.storage.db import connect
from dexpaprika.storage.migrations import migrate

PROBE = json.loads(
    (Path(__file__).parent.parent / "probes" / "out" / "s6" / "portfolio.json").read_text()
)
WALLET = PROBE["wallet"]
RAW = {k.lower(): v for k, v in PROBE["raw_calls"].items()}
ZERO_WORD = "0x" + "0" * 64
T1, T2, T3 = (
    "2026-08-02T12:00:00+00:00",
    "2026-08-02T13:00:00+00:00",
    "2026-08-02T14:00:00+00:00",
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "t.db")
    migrate(connection)
    QuotaTracker(connection).ensure_providers()
    yield connection
    connection.close()


def replay_rpc(conn: sqlite3.Connection) -> EvmRpcClient:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "eth_blockNumber":
            result: Any = hex(PROBE["pin"] + 3)
        elif body["method"] == "eth_getBalance":
            result = RAW.get(f"native|{body['params'][0].lower()}", "0x0")
        else:
            tx = body["params"][0]
            result = RAW.get(f"{tx['to'].lower()}|{tx['data']}".lower(), ZERO_WORD)
        return httpx.Response(200, text=json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}))

    settings = Settings.load()
    clients = [
        httpx.Client(
            transport=httpx.MockTransport(handle),
            base_url=url,
            headers={"User-Agent": "dexpaprika/1.0"},
        )
        for url in settings.base_rpc_urls
    ]
    return EvmRpcClient(conn, "base", settings=settings, clients=clients, sleeper=lambda _s: None)


class TestAave:
    def test_read_account_exact_decimals(self, conn: sqlite3.Connection) -> None:
        rpc = replay_rpc(conn)
        account = read_account(rpc, WALLET, settings=Settings.load(), block=PROBE["pin"])
        assert account.total_collateral_usd == Decimal("12432.31971853")
        assert account.total_debt_usd == Decimal("4673.0365822")
        assert account.health_factor.quantize(Decimal("0.0001")) == Decimal("2.2082")
        assert account.liq_threshold_bps == 8300

    def test_record_lend_and_borrow_rows(self, conn: sqlite3.Connection) -> None:
        rpc = replay_rpc(conn)
        account = read_account(rpc, WALLET, settings=Settings.load(), block=PROBE["pin"])
        record_aave(conn, WALLET, account, T1)
        rows = conn.execute("SELECT kind, group_tag, venue FROM positions ORDER BY kind").fetchall()
        assert [(r["kind"], r["group_tag"], r["venue"]) for r in rows] == [
            ("borrow", "defi", "aave-v3"),
            ("lend", "defi", "aave-v3"),
        ]
        state = json.loads(
            conn.execute("SELECT state_json FROM position_events").fetchone()["state_json"]
        )
        assert "health_factor" in state


class TestHoldings:
    def test_read_holdings_from_fixture(self, conn: sqlite3.Connection) -> None:
        rpc = replay_rpc(conn)
        holdings = read_holdings(rpc, "base", WALLET, block=PROBE["pin"])
        by_symbol = {h.symbol: h for h in holdings}
        assert by_symbol["ETH"].amount.quantize(Decimal("0.0001")) == Decimal("0.0969")
        assert by_symbol["USDC"].amount == Decimal("96.71236")
        # AERO raw 3099251453 at 18 decimals = dust (3.099e-9) — still tracked, >0.
        assert by_symbol["AERO"].amount == Decimal("3099251453") / Decimal(10**18)
        assert all(h.amount > 0 for h in holdings)

    def test_record_holdings(self, conn: sqlite3.Connection) -> None:
        rpc = replay_rpc(conn)
        holdings = read_holdings(rpc, "base", WALLET, block=PROBE["pin"])
        record_holdings(conn, WALLET, "base", holdings, T1)
        rows = conn.execute("SELECT * FROM positions WHERE kind='holding'").fetchall()
        assert len(rows) == len(holdings)
        assert all(r["group_tag"] == "holdings" for r in rows)


def _make_position(conn: sqlite3.Connection, external_id: str = "x:1") -> int:
    conn.execute(
        "INSERT INTO positions (wallet_ref, venue, chain, kind, external_id, group_tag)"
        " VALUES (?, 'test', 'base', 'lp', ?, 'lp_hedge')",
        (WALLET, external_id),
    )
    return int(
        conn.execute("SELECT id FROM positions WHERE external_id = ?", (external_id,)).fetchone()[
            "id"
        ]
    )


def _observe_state(
    conn: sqlite3.Connection, position_id: int, state: dict[str, Any], ts: str
) -> None:
    conn.execute(
        "INSERT INTO position_events (position_id, ts, type, delta_json, state_json)"
        " VALUES (?, ?, 'observed', '{}', ?)",
        (position_id, ts, json.dumps(state)),
    )
    observe(conn, position_id, ts)


def _events(conn: sqlite3.Connection, position_id: int) -> list[tuple[str, str]]:
    return [
        (r["type"], r["ts"])
        for r in conn.execute(
            "SELECT type, ts FROM position_events WHERE position_id=? ORDER BY id",
            (position_id,),
        )
    ]


class TestLifecycle:
    def test_first_observation_derives_open(self, conn: sqlite3.Connection) -> None:
        pid = _make_position(conn)
        _observe_state(conn, pid, {"liquidity": 100}, T1)
        assert [t for t, _ in _events(conn, pid)] == ["observed", "open"]

    def test_unchanged_observation_adds_nothing(self, conn: sqlite3.Connection) -> None:
        pid = _make_position(conn)
        _observe_state(conn, pid, {"liquidity": 100}, T1)
        _observe_state(conn, pid, {"liquidity": 100}, T2)
        assert [t for t, _ in _events(conn, pid)] == ["observed", "open", "observed"]

    def test_metric_change_derives_modify_with_delta(self, conn: sqlite3.Connection) -> None:
        pid = _make_position(conn)
        _observe_state(conn, pid, {"liquidity": 100}, T1)
        _observe_state(conn, pid, {"liquidity": 250}, T2)
        rows = conn.execute(
            "SELECT type, delta_json FROM position_events WHERE position_id=? ORDER BY id",
            (pid,),
        ).fetchall()
        assert [r["type"] for r in rows] == ["observed", "open", "observed", "modify"]
        delta = json.loads(rows[-1]["delta_json"])
        assert delta == {"field": "liquidity", "old": 100, "new": 250}

    def test_perp_size_change_tracked(self, conn: sqlite3.Connection) -> None:
        pid = _make_position(conn, "gmx:1")
        _observe_state(conn, pid, {"size_tokens": "7.03"}, T1)
        _observe_state(conn, pid, {"size_tokens": "5.00"}, T2)
        types = [t for t, _ in _events(conn, pid)]
        assert types[-1] == "modify"

    def test_reconcile_closures_once(self, conn: sqlite3.Connection) -> None:
        pid = _make_position(conn, "x:closed")
        _observe_state(conn, pid, {"liquidity": 100}, T1)
        reconcile_closures(conn, WALLET, "test", "lp", present_external_ids=[], ts=T2)
        reconcile_closures(conn, WALLET, "test", "lp", present_external_ids=[], ts=T3)
        types = [t for t, _ in _events(conn, pid)]
        assert types.count("full_close") == 1
        row = conn.execute("SELECT closed_at FROM positions WHERE id=?", (pid,)).fetchone()
        assert row["closed_at"] == T2

    def test_present_positions_not_closed(self, conn: sqlite3.Connection) -> None:
        pid = _make_position(conn, "x:live")
        _observe_state(conn, pid, {"liquidity": 100}, T1)
        reconcile_closures(conn, WALLET, "test", "lp", present_external_ids=["x:live"], ts=T2)
        assert "full_close" not in [t for t, _ in _events(conn, pid)]

    def test_events_replay_to_end_state(self, conn: sqlite3.Connection) -> None:
        """Property from the spec: the event stream reconstructs the final state."""
        pid = _make_position(conn)
        states = [{"liquidity": v} for v in (100, 100, 250, 300)]
        for i, state in enumerate(states):
            _observe_state(conn, pid, state, f"2026-08-02T1{i}:00:00+00:00")
        last_observed = conn.execute(
            "SELECT state_json FROM position_events WHERE position_id=? AND type='observed'"
            " ORDER BY id DESC LIMIT 1",
            (pid,),
        ).fetchone()
        assert json.loads(last_observed["state_json"]) == states[-1]
        modifies = conn.execute(
            "SELECT delta_json FROM position_events WHERE position_id=? AND type='modify'"
            " ORDER BY id",
            (pid,),
        ).fetchall()
        value = 100  # replay: open at 100, apply deltas
        for row in modifies:
            delta = json.loads(row["delta_json"])
            assert delta["old"] == value
            value = delta["new"]
        assert value == 300
