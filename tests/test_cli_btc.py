"""CLI snapshot/report with BTC holdings (S5.5) — mocked end-to-end."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from dexpaprika.cli import EXIT_OK, main

ROOT = Path(__file__).parent.parent / "probes" / "out"
S5 = json.loads((ROOT / "s5" / "discovery.json").read_text())
S6 = json.loads((ROOT / "s6" / "portfolio.json").read_text())
BTC = json.loads((ROOT / "s55" / "address_stats.json").read_text())
BTC_ADDRESS = BTC["address"]
BTC_PAYLOAD = json.dumps(BTC["blockstream"]["payload"])
EVM_WALLET = S5["wallet"]
RAW = {k.lower(): v for k, v in {**S5["raw_calls"], **S6["raw_calls"]}.items()}
ZERO_WORD = "0x" + "0" * 64


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEXPAPRIKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")
    monkeypatch.setenv("DEXPAPRIKA_BASE_RPC_URLS", "https://base-rpc.publicnode.com")


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

    def esplora_handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/address/{BTC_ADDRESS}"):
            return httpx.Response(200, text=BTC_PAYLOAD)
        return httpx.Response(404, text="nope")

    def fake_client(base_url: str) -> httpx.Client:
        if "blockstream" in base_url or "mempool" in base_url:
            handler = esplora_handle
        else:
            handler = rpc_handle
        return httpx.Client(transport=httpx.MockTransport(handler), base_url=base_url)

    monkeypatch.setattr("dexpaprika.cli._http_client_factory", fake_client)


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_holdings_snapshot_includes_btc(
    capsys: pytest.CaptureFixture[str], mock_everything: None
) -> None:
    run_json(capsys, "db", "migrate")
    run_json(capsys, "wallets", "add", "--chain", "evm", "--address", EVM_WALLET)
    run_json(capsys, "wallets", "add", "--chain", "btc", "--address", BTC_ADDRESS)
    code, out = run_json(capsys, "snapshot", "--kind", "holdings")
    assert code == EXIT_OK
    recorded = out["recorded"]
    assert isinstance(recorded, dict)
    assert recorded["holdings"] == 5  # 4 EVM (native+3 tokens) + 1 BTC

    code, report = run_json(capsys, "report")
    assert code == EXIT_OK
    groups = report["groups"]
    assert isinstance(groups, dict)
    btc_rows = [h for h in groups["holdings"] if h.get("symbol") == "BTC"]
    assert len(btc_rows) == 1
    assert btc_rows[0]["chain"] == "bitcoin"
    assert btc_rows[0]["amount"] == "0.00131828"


def test_btc_only_registry_snapshots_holdings(
    capsys: pytest.CaptureFixture[str], mock_everything: None
) -> None:
    """A registry with ONLY a BTC wallet must not error for holdings."""
    run_json(capsys, "db", "migrate")
    run_json(capsys, "wallets", "add", "--chain", "btc", "--address", BTC_ADDRESS)
    code, out = run_json(capsys, "snapshot", "--kind", "holdings")
    assert code == EXIT_OK
    recorded = out["recorded"]
    assert isinstance(recorded, dict)
    assert recorded["holdings"] == 1


def test_excluded_btc_wallet_skipped(
    capsys: pytest.CaptureFixture[str], mock_everything: None
) -> None:
    run_json(capsys, "db", "migrate")
    run_json(capsys, "wallets", "add", "--chain", "evm", "--address", EVM_WALLET)
    run_json(capsys, "wallets", "add", "--chain", "btc", "--address", BTC_ADDRESS)
    run_json(capsys, "wallets", "exclude", "--address", BTC_ADDRESS)
    code, out = run_json(capsys, "snapshot", "--kind", "holdings")
    assert code == EXIT_OK
    recorded = out["recorded"]
    assert isinstance(recorded, dict)
    assert recorded["holdings"] == 4  # EVM only
