"""S12a — recorder cycle + service + full-variable capture (offline).

Zero-network: base RPC / GMX / DexPaprika are httpx.MockTransport replays of the
committed probe fixtures; the service loop is driven by an injected clock+sleep.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from dexpaprika.config import Settings
from dexpaprika.recorder import (
    CycleResult,
    RecorderClients,
    RecorderService,
    SourceStamp,
    build_clients,
    run_cycle,
)
from dexpaprika.storage.db import connect
from dexpaprika.storage.migrations import migrate

ROOT = Path(__file__).parent.parent / "probes" / "out"
S5 = json.loads((ROOT / "s5" / "discovery.json").read_text())
GMX_POS = (ROOT / "s4" / "positions_peer_io.json").read_text()
GMX_MARKETS = (ROOT / "s4" / "markets.json").read_text()
POOL_DETAILS = (ROOT / "s3" / "pool_details.json").read_text()
WALLET = S5["wallet"]
POOL = S5["resolved_pool"]
RAW = {k.lower(): v for k, v in S5["raw_calls"].items()}
ZERO_WORD = "0x" + "0" * 64
T0 = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)


def _factory(base_url: str) -> httpx.Client:
    def rpc_handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "eth_blockNumber":
            result: Any = hex(S5["pin"] + 3)
        elif body["method"] == "eth_getBalance":
            result = RAW.get(f"native|{body['params'][0].lower()}", "0x0")
        else:
            tx = body["params"][0]
            result = RAW.get(f"{tx['to'].lower()}|{tx['data']}".lower(), ZERO_WORD)
        return httpx.Response(200, text=json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}))

    def gmx_handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/positions"):
            return httpx.Response(200, text=GMX_POS)
        if request.url.path.endswith("/markets"):
            return httpx.Response(200, text=GMX_MARKETS)
        return httpx.Response(404, text="nope")

    def paprika_handle(request: httpx.Request) -> httpx.Response:
        if "/pools/" in request.url.path:
            return httpx.Response(200, text=POOL_DETAILS)
        return httpx.Response(404, text="nope")

    if "gmxapi" in base_url:
        handler = gmx_handle
    elif "dexpaprika" in base_url:
        handler = paprika_handle
    else:
        handler = rpc_handle
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=base_url,
        headers={"User-Agent": "dexpaprika/1.0"},
    )


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "rec.db")
    migrate(connection)
    from dexpaprika.quota import QuotaTracker

    QuotaTracker(connection).ensure_providers()
    return connection


def _settings() -> Settings:
    return Settings.load()


def _clients(conn: sqlite3.Connection, kinds: list[str]) -> RecorderClients:
    return build_clients(conn, _settings(), kinds=kinds, wallets=[WALLET], client_factory=_factory)


def _observed(conn: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT e.state_json FROM positions p JOIN position_events e ON e.position_id = p.id"
        " WHERE p.kind = ? AND e.type = 'observed' ORDER BY e.id",
        (kind,),
    ).fetchall()
    return [json.loads(r["state_json"]) for r in rows]


# --------------------------- 1. run_cycle basics ---------------------------


def test_run_cycle_records_snapshots_events_and_heartbeat(conn: sqlite3.Connection) -> None:
    kinds = ["lp", "hedge"]
    result = run_cycle(
        conn, _settings(), kinds=kinds, wallets=[WALLET], now=T0, clients=_clients(conn, kinds)
    )
    assert result.counts == {"lp": 1, "hedge": 1}
    assert result.all_ok()
    assert result.sources["lp"].block == S5["pin"]  # resolve_pin applies confirmations
    assert result.sources["hedge"].block is None  # hedge carries no chain block

    snap = {
        r["kind"]: r["chain"] for r in conn.execute("SELECT kind, chain FROM snapshots").fetchall()
    }
    assert snap == {"lp": "base", "hedge": "arbitrum"}
    hb = conn.execute("SELECT kind, ok FROM recorder_heartbeat ORDER BY kind").fetchall()
    assert {(r["kind"], r["ok"]) for r in hb} == {("hedge", 1), ("lp", 1)}
    assert len(_observed(conn, "lp")) == 1
    assert len(_observed(conn, "perp")) == 1


def test_failing_source_is_isolated_not_fatal(conn: sqlite3.Connection) -> None:
    # GMX peer that always 500s → hedge fails, lp still recorded, loop returns.
    def bad_gmx_factory(base_url: str) -> httpx.Client:
        if "gmxapi" in base_url:
            return httpx.Client(
                transport=httpx.MockTransport(lambda r: httpx.Response(500, text="down")),
                base_url=base_url,
            )
        return _factory(base_url)

    clients = build_clients(
        conn, _settings(), kinds=["lp", "hedge"], wallets=[WALLET], client_factory=bad_gmx_factory
    )
    result = run_cycle(
        conn, _settings(), kinds=["lp", "hedge"], wallets=[WALLET], now=T0, clients=clients
    )
    assert result.counts == {"lp": 1}  # hedge absent
    assert not result.all_ok()
    assert result.sources["lp"].ok is True
    assert result.sources["hedge"].ok is False
    # Heartbeat recorded the failure; no partial perp rows.
    hb = {r["kind"]: r["ok"] for r in conn.execute("SELECT kind, ok FROM recorder_heartbeat")}
    assert hb == {"lp": 1, "hedge": 0}
    assert _observed(conn, "perp") == []
    assert len(_observed(conn, "lp")) == 1


# --------------------------- 2. full-variable capture ---------------------------


def test_lp_state_has_full_variable_set(conn: sqlite3.Connection) -> None:
    run_cycle(
        conn, _settings(), kinds=["lp"], wallets=[WALLET], now=T0, clients=_clients(conn, ["lp"])
    )
    (lp,) = _observed(conn, "lp")
    # on-chain
    for field in (
        "liquidity",
        "tick_lower",
        "tick_upper",
        "amount0",
        "amount1",
        "in_range",
        "tokens_owed0",
        "tokens_owed1",
        "pool_tick",
        "sqrt_price_x96",
        "price_usd",
    ):
        assert field in lp, field
    # S12a additions
    assert lp["token0_price_usd"] is not None
    assert lp["token1_price_usd"] == "1"  # USDC numeraire
    assert lp["pool_volume_usd_24h"] is not None
    # pool volume came from the DexPaprika fixture (24h.volume_usd)
    assert lp["pool_volume_usd_24h"].startswith("16542.")


def test_hedge_state_has_sl_size_and_all_fields(conn: sqlite3.Connection) -> None:
    run_cycle(
        conn,
        _settings(),
        kinds=["hedge"],
        wallets=[WALLET],
        now=T0,
        clients=_clients(conn, ["hedge"]),
    )
    (perp,) = _observed(conn, "perp")
    for field in (
        "mark_price",
        "entry_price",
        "liquidation_price",
        "size_usd",
        "size_tokens",
        "collateral_usd",
        "collateral_amount",
        "leverage",
        "pnl",
        "pending_funding_fees_usd",
        "pending_borrowing_fees_usd",
    ):
        assert field in perp, field
    assert perp["stop_loss_triggers"]  # existing trigger list
    sl = perp["stop_loss_orders"]
    assert isinstance(sl, list) and sl
    entry = sl[0]
    assert set(entry) == {"trigger", "size_usd", "is_full_close"}
    assert entry["trigger"] is not None
    # SL SIZE is now co-located in the hedge state (the S12a gap closed).
    assert entry["size_usd"] is not None or entry["is_full_close"] is True


# --------------------------- 3. service cadence ---------------------------


class _Clock:
    def __init__(self, start: datetime, step: float) -> None:
        self.t = start
        self.step = step

    def __call__(self) -> datetime:
        v = self.t
        self.t = self.t + timedelta(seconds=self.step)
        return v


def _fake_cycle_calls() -> tuple[list[tuple[datetime, list[str]]], Any]:
    calls: list[tuple[datetime, list[str]]] = []

    def fake(
        conn: Any,
        settings: Any,
        *,
        kinds: Any,
        wallets: Any,
        btc_wallets: Any,
        now: datetime,
        clients: Any,
    ) -> CycleResult:
        ks = list(kinds)
        calls.append((now, ks))
        return CycleResult(
            ts=now.isoformat(),
            wallets=[],
            counts=dict.fromkeys(ks, 1),
            sources={k: SourceStamp(ok=True, ts=now.isoformat()) for k in ks},
        )

    return calls, fake


def test_service_runs_each_source_on_its_own_cadence(conn: sqlite3.Connection) -> None:
    calls, fake = _fake_cycle_calls()
    svc = RecorderService(
        conn,
        _settings(),
        kinds=["lp", "hedge"],
        wallets=[WALLET],
        intervals={"lp": 10, "hedge": 30},
        clock=_Clock(T0, 10),
        sleep=lambda _s: None,
        cycle_fn=fake,
    )
    status = svc.run(max_cycles=6)
    lp_ticks = [t for t, ks in calls if "lp" in ks]
    hedge_ticks = [t for t, ks in calls if "hedge" in ks]
    assert len(lp_ticks) == 6  # every tick (10s interval, 10s step)
    assert hedge_ticks == [T0, T0 + timedelta(seconds=30)]  # 0 and +30
    assert status.cycles == 6


# --------------------------- 4. backoff ---------------------------


def test_failed_source_retries_on_backoff_and_loop_survives(conn: sqlite3.Connection) -> None:
    calls: list[tuple[datetime, list[str]]] = []

    def fake(
        conn: Any,
        settings: Any,
        *,
        kinds: Any,
        wallets: Any,
        btc_wallets: Any,
        now: datetime,
        clients: Any,
    ) -> CycleResult:
        ks = list(kinds)
        calls.append((now, ks))
        sources = {}
        for k in ks:
            ok = k != "hedge"  # hedge always fails
            sources[k] = SourceStamp(ok=ok, ts=now.isoformat(), error=None if ok else "boom")
        return CycleResult(ts=now.isoformat(), wallets=[], counts={}, sources=sources)

    svc = RecorderService(
        conn,
        _settings(),
        kinds=["lp", "hedge"],
        wallets=[WALLET],
        intervals={"lp": 10, "hedge": 30},
        clock=_Clock(T0, 10),
        sleep=lambda _s: None,
        cycle_fn=fake,
        base_backoff=5.0,
    )
    status = svc.run(max_cycles=4)
    assert status.cycles == 4  # loop survived every failure
    # Capped exponential backoff from 5s: retries at +5 (tick1), +10 (tick2),
    # then +20 pushes past tick3 — so ticks 0,1,2, faster than the 30s interval.
    hedge_ticks = [t for t, ks in calls if "hedge" in ks]
    assert hedge_ticks == [T0, T0 + timedelta(seconds=10), T0 + timedelta(seconds=20)]
    assert status.sources["hedge"].ok is False
    assert status.sources["lp"].ok is True  # other source unaffected


# --------------------------- 5. staleness ---------------------------


def test_status_staleness_grows_and_failed_source_keeps_stale_stamp(
    conn: sqlite3.Connection,
) -> None:
    _calls, fake = _fake_cycle_calls()
    svc = RecorderService(
        conn,
        _settings(),
        kinds=["lp"],
        wallets=[WALLET],
        intervals={"lp": 10},
        clock=_Clock(T0, 10),
        sleep=lambda _s: None,
        cycle_fn=fake,
    )
    svc.run(max_cycles=1)  # one lp cycle stamped at T0
    later = T0 + timedelta(seconds=45)
    status = svc.status(now=later)
    assert status.staleness_seconds["lp"] == pytest.approx(45.0)
    assert status.sources["lp"].ts == T0.isoformat()


# --------------------------- 6. cycle == service tick (equivalence) ---------------------------


def test_sequential_run_cycle_equals_service_run(tmp_path: Path) -> None:
    def build_db() -> sqlite3.Connection:
        c = connect(tmp_path / f"eq_{id(object())}.db")
        migrate(c)
        from dexpaprika.quota import QuotaTracker

        QuotaTracker(c).ensure_providers()
        return c

    def counts(c: sqlite3.Connection) -> tuple[int, int, int]:
        obs = c.execute("SELECT COUNT(*) n FROM position_events WHERE type='observed'").fetchone()[
            "n"
        ]
        snaps = c.execute("SELECT COUNT(*) n FROM snapshots").fetchone()["n"]
        hb = c.execute("SELECT COUNT(*) n FROM recorder_heartbeat").fetchone()["n"]
        return obs, snaps, hb

    # A) two sequential run_cycle calls
    a = build_db()
    for i in range(2):
        run_cycle(
            a,
            _settings(),
            kinds=["lp", "hedge"],
            wallets=[WALLET],
            now=T0 + timedelta(seconds=i),
            clients=_clients(a, ["lp", "hedge"]),
        )
    # B) one service run, max_cycles=2, always-due (interval 0)
    b = build_db()
    svc = RecorderService(
        b,
        _settings(),
        kinds=["lp", "hedge"],
        wallets=[WALLET],
        intervals={"lp": 0, "hedge": 0},
        clock=_Clock(T0, 1),
        sleep=lambda _s: None,
        clients=_clients(b, ["lp", "hedge"]),
    )
    svc.run(max_cycles=2)
    assert counts(a) == counts(b)


# --------------------------- 7. CLI contracts ---------------------------


@pytest.fixture
def _cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEXPAPRIKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")
    monkeypatch.setattr("dexpaprika.cli._http_client_factory", _factory)


def _run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    from dexpaprika.cli import main

    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_cli_recorder_cycle_status_run(capsys: pytest.CaptureFixture[str], _cli_env: None) -> None:
    from dexpaprika.cli import EXIT_OK

    _run_cli(capsys, "db", "migrate")
    _run_cli(capsys, "wallets", "add", "--chain", "evm", "--address", WALLET)

    code, out = _run_cli(capsys, "recorder", "cycle", "--kind", "hedge", "--address", WALLET)
    assert code == EXIT_OK
    assert out["recorded"] == {"hedge": 1}
    assert out["ok"] is True

    code, out = _run_cli(capsys, "recorder", "status")
    assert code == EXIT_OK
    assert out["sources"]["hedge"]["ok"] is True
    assert "staleness_seconds" in out["sources"]["hedge"]

    code, out = _run_cli(
        capsys, "recorder", "run", "--kind", "hedge", "--address", WALLET, "--max-cycles", "1"
    )
    assert code == EXIT_OK
    assert out["cycles"] == 1
