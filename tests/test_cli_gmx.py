"""CLI gmx group — default-address resolution + record, mocked transports."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from dexpaprika.cli import EXIT_FAILURE, EXIT_OK, main

FIXTURES = Path(__file__).parent.parent / "probes" / "out" / "s4"
WALLET = "0xC155A616e39D7B83E37e8FD9d2106E1BC056d7Fe"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEXPAPRIKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")


@pytest.fixture
def mock_api(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/positions"):
            return httpx.Response(200, text=(FIXTURES / "positions_peer_io.json").read_text())
        if path.endswith("/markets"):
            return httpx.Response(200, text=(FIXTURES / "markets.json").read_text())
        return httpx.Response(404, text="not found")

    def fake_client(base_url: str) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handle), base_url=base_url)

    monkeypatch.setattr("dexpaprika.cli._http_client_factory", fake_client)


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_gmx_positions_explicit_address(capsys: pytest.CaptureFixture[str], mock_api: None) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(capsys, "gmx", "positions", "--address", WALLET)
    assert code == EXIT_OK
    positions = out["positions"]
    assert isinstance(positions, list)
    p = positions[0]
    assert isinstance(p, dict)
    assert p["index_name"] == "ETH/USD"
    from decimal import Decimal

    # Exactness is the contract; string form may carry trailing zeros.
    assert Decimal(str(p["size_usd"])) == Decimal("13155.762269646219571243906932")


def test_gmx_positions_default_address_from_registry(
    capsys: pytest.CaptureFixture[str], mock_api: None
) -> None:
    run_json(capsys, "db", "migrate")
    run_json(capsys, "wallets", "add", "--chain", "evm", "--address", WALLET)
    code, out = run_json(capsys, "gmx", "positions")
    assert code == EXIT_OK
    assert out["address"] == WALLET


def test_gmx_positions_no_wallet_is_actionable(
    capsys: pytest.CaptureFixture[str], mock_api: None
) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(capsys, "gmx", "positions")
    assert code == EXIT_FAILURE
    assert "--address" in str(out["error"])


def test_gmx_positions_record(capsys: pytest.CaptureFixture[str], mock_api: None) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(capsys, "gmx", "positions", "--address", WALLET, "--record")
    assert code == EXIT_OK
    assert out["recorded"] == 1


def test_gmx_requires_migrated_db(capsys: pytest.CaptureFixture[str], mock_api: None) -> None:
    code, out = run_json(capsys, "gmx", "positions", "--address", WALLET)
    assert code == EXIT_FAILURE
    assert "migrate" in str(out["error"])
