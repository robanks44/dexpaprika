"""CLI lp snapshot — replayed probe fixtures end-to-end."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from dexpaprika.cli import EXIT_FAILURE, EXIT_OK, main

PROBE = json.loads(
    (Path(__file__).parent.parent / "probes" / "out" / "s5" / "discovery.json").read_text()
)
WALLET = PROBE["wallet"]
RAW = {k.lower(): v for k, v in PROBE["raw_calls"].items()}
ZERO_WORD = "0x" + "0" * 64


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEXPAPRIKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")
    monkeypatch.setenv("DEXPAPRIKA_BASE_RPC_URLS", "https://base-rpc.publicnode.com")


@pytest.fixture
def mock_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "eth_blockNumber":
            result: Any = hex(PROBE["pin"] + 3)
        else:
            tx = body["params"][0]
            result = RAW.get(f"{tx['to'].lower()}|{tx['data']}".lower(), ZERO_WORD)
        return httpx.Response(200, text=json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}))

    def fake_client(base_url: str) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(handle),
            base_url=base_url,
            headers={"User-Agent": "dexpaprika/1.0"},
        )

    monkeypatch.setattr("dexpaprika.cli._http_client_factory", fake_client)


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_lp_snapshot_explicit_address(capsys: pytest.CaptureFixture[str], mock_rpc: None) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(capsys, "lp", "snapshot", "--address", WALLET)
    assert code == EXIT_OK
    positions = out["positions"]
    assert isinstance(positions, list)
    assert len(positions) == 1
    p = positions[0]
    assert isinstance(p, dict)
    assert p["token_id"] == 5056427
    assert p["custody"] == "sickle"
    assert p["in_range"] is True
    assert out["block_number"] == PROBE["pin"]


def test_lp_snapshot_registry_wallets_and_record(
    capsys: pytest.CaptureFixture[str], mock_rpc: None
) -> None:
    run_json(capsys, "db", "migrate")
    run_json(capsys, "wallets", "add", "--chain", "evm", "--address", WALLET)
    code, out = run_json(capsys, "lp", "snapshot", "--record")
    assert code == EXIT_OK
    assert out["recorded"] == 1


def test_lp_snapshot_no_wallets_is_actionable(
    capsys: pytest.CaptureFixture[str], mock_rpc: None
) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(capsys, "lp", "snapshot")
    assert code == EXIT_FAILURE
    assert "--address" in str(out["error"]) or "wallet" in str(out["error"])


def test_lp_requires_migrated_db(capsys: pytest.CaptureFixture[str], mock_rpc: None) -> None:
    code, out = run_json(capsys, "lp", "snapshot", "--address", WALLET)
    assert code == EXIT_FAILURE
    assert "migrate" in str(out["error"])
