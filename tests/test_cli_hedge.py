"""CLI hedge status/simulate — recorded-state end-to-end."""

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


def _snapshot(capsys: pytest.CaptureFixture[str]) -> None:
    run_json(capsys, "db", "migrate")
    run_json(capsys, "wallets", "add", "--chain", "evm", "--address", WALLET)
    run_json(capsys, "snapshot", "--kind", "lp")
    run_json(capsys, "snapshot", "--kind", "hedge")


def test_hedge_status_from_recorded_states(
    capsys: pytest.CaptureFixture[str], mock_everything: None
) -> None:
    _snapshot(capsys)
    code, out = run_json(capsys, "hedge", "status")
    assert code == EXIT_OK
    analysis = out["analysis"]
    assert isinstance(analysis, dict)
    assert analysis["quadrant"] in ("Q1", "Q2", "Q3", "Q4")
    assert "over-hedged" in analysis["flags"]  # 7.04 short vs ~5.03 delta
    assert "sl-correlated-with-top-exit" in analysis["flags"]
    assert analysis["coverage_ratio_eth"] is not None


def test_hedge_status_requires_recorded_data(
    capsys: pytest.CaptureFixture[str], mock_everything: None
) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(capsys, "hedge", "status")
    assert code == EXIT_FAILURE
    assert "snapshot" in str(out["error"])


def test_hedge_simulate_curve(capsys: pytest.CaptureFixture[str], mock_everything: None) -> None:
    _snapshot(capsys)
    code, out = run_json(capsys, "hedge", "simulate", "--curve", "5")
    assert code == EXIT_OK
    points = out["points"]
    assert isinstance(points, list)
    assert len(points) == 5
    first = points[0]
    assert isinstance(first, dict)
    assert {"price_usd", "lp_value_usd", "short_pnl_usd", "net_usd", "quadrant"} <= set(first)


def test_hedge_simulate_single_price(
    capsys: pytest.CaptureFixture[str], mock_everything: None
) -> None:
    _snapshot(capsys)
    code, out = run_json(capsys, "hedge", "simulate", "--price", "1700")
    assert code == EXIT_OK
    points = out["points"]
    assert isinstance(points, list)
    assert len(points) == 1
