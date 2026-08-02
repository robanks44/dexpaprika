"""CLI market group — mocked transport end-to-end."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from dexpaprika.cli import EXIT_FAILURE, EXIT_OK, main

FIXTURES = Path(__file__).parent.parent / "probes" / "out" / "s3"
POOL = "0x56aeaf4af2df4bdfd9d865830fefdd278b25e7ef"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEXPAPRIKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")


@pytest.fixture
def mock_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route CLI-constructed clients through recorded probe payloads."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/ohlcv"):
            return httpx.Response(200, text=(FIXTURES / "ohlcv_24h.json").read_text())
        if path.endswith(f"/pools/{POOL}"):
            return httpx.Response(200, text=(FIXTURES / "pool_details.json").read_text())
        return httpx.Response(404, text="not found")

    def fake_client(base_url: str) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handle), base_url=base_url)

    monkeypatch.setattr("dexpaprika.cli._http_client_factory", fake_client)


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_market_pool(capsys: pytest.CaptureFixture[str], mock_api: None) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(capsys, "market", "pool", "--network", "base", "--address", POOL)
    assert code == EXIT_OK
    pool = out["pool"]
    assert isinstance(pool, dict)
    assert pool["dex_id"] == "aerodrome_slipstream_2"
    assert pool["fee"] is None


def test_market_pool_record(capsys: pytest.CaptureFixture[str], mock_api: None) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(
        capsys, "market", "pool", "--network", "base", "--address", POOL, "--record"
    )
    assert code == EXIT_OK
    assert out["recorded"] is True


def test_market_ohlcv_record(capsys: pytest.CaptureFixture[str], mock_api: None) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(
        capsys,
        "market",
        "ohlcv",
        "--network",
        "base",
        "--address",
        POOL,
        "--start",
        "2026-07-26",
        "--record",
    )
    assert code == EXIT_OK
    assert isinstance(out["candles"], list)
    assert out["recorded"] >= 5


def test_market_record_requires_migrated_db(
    capsys: pytest.CaptureFixture[str], mock_api: None
) -> None:
    code, out = run_json(
        capsys, "market", "pool", "--network", "base", "--address", POOL, "--record"
    )
    assert code == EXIT_FAILURE
    assert "migrate" in str(out["error"])


def test_market_upstream_error_is_clean(capsys: pytest.CaptureFixture[str], mock_api: None) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(capsys, "market", "pool", "--network", "base", "--address", "0xdead")
    assert code == EXIT_FAILURE
    assert "error" in out
