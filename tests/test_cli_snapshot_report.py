"""CLI snapshot orchestrator + report — combined replay of all probe fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from dexpaprika.cli import EXIT_FAILURE, EXIT_OK, main

ROOT = Path(__file__).parent.parent / "probes" / "out"
S5 = json.loads((ROOT / "s5" / "discovery.json").read_text())
S6 = json.loads((ROOT / "s6" / "portfolio.json").read_text())
GMX_FIXTURE = (ROOT / "s4" / "positions_peer_io.json").read_text()
GMX_MARKETS = (ROOT / "s4" / "markets.json").read_text()
WALLET = S5["wallet"]
RAW = {k.lower(): v for k, v in {**S5["raw_calls"], **S6["raw_calls"]}.items()}
ZERO_WORD = "0x" + "0" * 64


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEXPAPRIKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")
    monkeypatch.setenv("DEXPAPRIKA_BASE_RPC_URLS", "https://base-rpc.publicnode.com")
    monkeypatch.setenv("DEXPAPRIKA_ARBITRUM_RPC_URLS", "https://arb1.arbitrum.io/rpc")


@pytest.fixture
def mock_everything(monkeypatch: pytest.MonkeyPatch) -> None:
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
            return httpx.Response(200, text=GMX_FIXTURE)
        if request.url.path.endswith("/markets"):
            return httpx.Response(200, text=GMX_MARKETS)
        return httpx.Response(404, text="nope")

    def fake_client(base_url: str) -> httpx.Client:
        handler = gmx_handle if "gmxapi" in base_url else rpc_handle
        return httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url=base_url,
            headers={"User-Agent": "dexpaprika/1.0"},
        )

    monkeypatch.setattr("dexpaprika.cli._http_client_factory", fake_client)


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def _setup(capsys: pytest.CaptureFixture[str]) -> None:
    run_json(capsys, "db", "migrate")
    run_json(capsys, "wallets", "add", "--chain", "evm", "--address", WALLET)


def test_snapshot_all_records_every_group(
    capsys: pytest.CaptureFixture[str], mock_everything: None
) -> None:
    _setup(capsys)
    code, out = run_json(capsys, "snapshot", "--kind", "all")
    assert code == EXIT_OK
    recorded = out["recorded"]
    assert isinstance(recorded, dict)
    assert recorded["lp"] == 1
    assert recorded["hedge"] == 1
    assert recorded["defi"] == 2  # lend + borrow
    assert int(str(recorded["holdings"])) >= 3


def test_snapshot_is_idempotent(capsys: pytest.CaptureFixture[str], mock_everything: None) -> None:
    _setup(capsys)
    run_json(capsys, "snapshot", "--kind", "all")
    code, _out = run_json(capsys, "snapshot", "--kind", "all")
    assert code == EXIT_OK
    # Same positions, no duplicates; events appended.
    import sqlite3

    from dexpaprika.config import Settings
    from dexpaprika.storage.db import db_path

    conn = sqlite3.connect(db_path(Settings.load()))
    try:
        positions = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        opens = conn.execute("SELECT COUNT(*) FROM position_events WHERE type='open'").fetchone()[0]
    finally:
        conn.close()
    assert positions == opens  # every position opened exactly once across two runs


def test_snapshot_single_kind(capsys: pytest.CaptureFixture[str], mock_everything: None) -> None:
    _setup(capsys)
    code, out = run_json(capsys, "snapshot", "--kind", "defi")
    assert code == EXIT_OK
    recorded = out["recorded"]
    assert isinstance(recorded, dict)
    assert list(recorded) == ["defi"]


def test_report_groups_and_as_of(capsys: pytest.CaptureFixture[str], mock_everything: None) -> None:
    _setup(capsys)
    run_json(capsys, "snapshot", "--kind", "all")
    code, out = run_json(capsys, "report")
    assert code == EXIT_OK
    groups = out["groups"]
    assert isinstance(groups, dict)
    assert set(groups) == {"lp_hedge", "defi", "holdings"}
    lp_entries = groups["lp_hedge"]
    assert isinstance(lp_entries, list)
    assert len(lp_entries) == 2  # LP + perp
    for group_entries in groups.values():
        assert isinstance(group_entries, list)
        for entry in group_entries:
            assert isinstance(entry, dict)
            assert entry["as_of"]
            assert entry["venue"]
    totals = out["totals_usd"]
    assert isinstance(totals, dict)
    assert "defi_net" in totals


def test_snapshot_requires_migrated_db(
    capsys: pytest.CaptureFixture[str], mock_everything: None
) -> None:
    code, out = run_json(capsys, "snapshot", "--kind", "all")
    assert code == EXIT_FAILURE
    assert "migrate" in str(out["error"])
