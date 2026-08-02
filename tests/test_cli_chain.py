"""CLI chain snapshot — mocked JSON-RPC end-to-end."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from dexpaprika.cli import EXIT_FAILURE, EXIT_OK, main

PROBE = json.loads(
    (Path(__file__).parent.parent / "probes" / "out" / "s45" / "pinned_multicall.json").read_text()
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEXPAPRIKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")
    # Single-URL rings so one mock serves each chain deterministically.
    monkeypatch.setenv("DEXPAPRIKA_BASE_RPC_URLS", "https://base-rpc.publicnode.com")
    monkeypatch.setenv("DEXPAPRIKA_ARBITRUM_RPC_URLS", "https://arb1.arbitrum.io/rpc")


@pytest.fixture
def mock_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    from dexpaprika.chains.abi import encode_uint

    def base_handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "eth_blockNumber":
            result: Any = hex(PROBE["base"]["pin"] + 3)
        else:
            result = PROBE["base"]["raw_response"]
        return httpx.Response(200, text=json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}))

    def arb_handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        pin = PROBE["arbitrum"]["arbsys_pin_used"]
        if body["method"] == "eth_blockNumber":
            result: Any = hex(pin + 3)
        else:
            inner = [encode_uint(pin), encode_uint(42161)]
            offsets, bodies, running = [], [], 32 * len(inner)
            for word in inner:
                offsets.append(f"{running:064x}")
                bodies.append(f"{32:064x}" + word)
                running += 64
            result = (
                "0x"
                + f"{25664170:064x}"
                + f"{64:064x}"
                + f"{len(inner):064x}"
                + "".join(offsets)
                + "".join(bodies)
            )
        return httpx.Response(200, text=json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}))

    def fake_client(base_url: str) -> httpx.Client:
        handler = arb_handle if "arb" in base_url else base_handle
        return httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url=base_url,
            headers={"User-Agent": "dexpaprika/1.0"},
        )

    monkeypatch.setattr("dexpaprika.cli._http_client_factory", fake_client)


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_chain_snapshot_all(capsys: pytest.CaptureFixture[str], mock_rpc: None) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(capsys, "chain", "snapshot")
    assert code == EXIT_OK
    chains = out["chains"]
    assert isinstance(chains, dict)
    assert chains["base"]["block_number"] == PROBE["base"]["pin"]  # type: ignore[index]
    assert chains["arbitrum"]["block_number"] == PROBE["arbitrum"]["arbsys_pin_used"]  # type: ignore[index]


def test_chain_snapshot_single(capsys: pytest.CaptureFixture[str], mock_rpc: None) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(capsys, "chain", "snapshot", "--chain", "base")
    assert code == EXIT_OK
    chains = out["chains"]
    assert isinstance(chains, dict)
    assert list(chains) == ["base"]


def test_chain_requires_migrated_db(capsys: pytest.CaptureFixture[str], mock_rpc: None) -> None:
    code, out = run_json(capsys, "chain", "snapshot")
    assert code == EXIT_FAILURE
    assert "migrate" in str(out["error"])
