"""CLI alerts check/test/log — recorded-state end-to-end with mocked ntfy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from dexpaprika.cli import EXIT_DEGRADED, EXIT_OK, main

ROOT = Path(__file__).parent.parent / "probes" / "out"
S5 = json.loads((ROOT / "s5" / "discovery.json").read_text())
S6 = json.loads((ROOT / "s6" / "portfolio.json").read_text())
GMX_FIXTURE = (ROOT / "s4" / "positions_peer_io.json").read_text()
GMX_MARKETS = (ROOT / "s4" / "markets.json").read_text()
NTFY_RECEIPT = json.loads((ROOT / "s8" / "publish_receipt.json").read_text())["receipt"]
WALLET = S5["wallet"]
RAW = {k.lower(): v for k, v in {**S5["raw_calls"], **S6["raw_calls"]}.items()}
ZERO_WORD = "0x" + "0" * 64
TOPIC = "secret-topic-abc123"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEXPAPRIKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")
    monkeypatch.setenv("DEXPAPRIKA_SECRET_NTFY_TOPIC", TOPIC)
    monkeypatch.setenv("DEXPAPRIKA_BASE_RPC_URLS", "https://base-rpc.publicnode.com")
    monkeypatch.setenv("DEXPAPRIKA_ARBITRUM_RPC_URLS", "https://arb1.arbitrum.io/rpc")


class NtfyRecorder:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, text=json.dumps(NTFY_RECEIPT))


@pytest.fixture
def ntfy() -> NtfyRecorder:
    return NtfyRecorder()


@pytest.fixture
def mock_everything(monkeypatch: pytest.MonkeyPatch, ntfy: NtfyRecorder) -> None:
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
        if "ntfy" in base_url:
            handler: Any = ntfy
        elif "gmxapi" in base_url:
            handler = gmx_handle
        else:
            handler = rpc_handle
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


def _log_rows(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    code, out = run_json(capsys, "alerts", "log")
    assert code == EXIT_OK
    rows = out["alerts"]
    assert isinstance(rows, list)
    return rows


def test_check_fires_delivers_and_records(
    capsys: pytest.CaptureFixture[str], mock_everything: None, ntfy: NtfyRecorder
) -> None:
    """Live fixture: over-hedged beyond band -> rebalance-needed, delivered, logged."""
    _snapshot(capsys)
    code, out = run_json(capsys, "alerts", "check")
    assert code == EXIT_OK
    fired = out["fired"]
    assert isinstance(fired, list)
    assert [a["rule"] for a in fired if isinstance(a, dict)] == ["rebalance-needed"]
    assert len(ntfy.requests) == 1
    body = json.loads(ntfy.requests[0].content)
    assert body["topic"] == TOPIC
    assert ntfy.requests[0].url.path == "/"

    [row] = _log_rows(capsys)
    assert row["rule"] == "rebalance-needed"
    assert row["delivered"] == 1
    # Topic never lands in the log.
    assert TOPIC not in json.dumps(row)


def test_check_cooldown_suppresses_second_run(
    capsys: pytest.CaptureFixture[str], mock_everything: None, ntfy: NtfyRecorder
) -> None:
    _snapshot(capsys)
    run_json(capsys, "alerts", "check")
    code, out = run_json(capsys, "alerts", "check")
    assert code == EXIT_OK
    assert out["fired"] == []
    suppressed = out["suppressed"]
    assert isinstance(suppressed, list)
    assert [a["rule"] for a in suppressed if isinstance(a, dict)] == ["rebalance-needed"]
    assert len(ntfy.requests) == 1  # no re-delivery
    assert len(_log_rows(capsys)) == 1  # no second row


def test_check_delivery_failure_recorded_not_lost(
    capsys: pytest.CaptureFixture[str], mock_everything: None, ntfy: NtfyRecorder
) -> None:
    ntfy.status = 500
    _snapshot(capsys)
    code, out = run_json(capsys, "alerts", "check")
    assert code == EXIT_DEGRADED
    assert out["degraded"] is True
    [row] = _log_rows(capsys)
    assert row["rule"] == "rebalance-needed"
    assert row["delivered"] == 0
    status = row["ntfy_status"]
    assert isinstance(status, str) and status
    assert TOPIC not in status


def test_check_dry_run_records_and_sends_nothing(
    capsys: pytest.CaptureFixture[str], mock_everything: None, ntfy: NtfyRecorder
) -> None:
    _snapshot(capsys)
    code, out = run_json(capsys, "alerts", "check", "--dry-run")
    assert code == EXIT_OK
    alerts = out["alerts"]
    assert isinstance(alerts, list)
    assert [a["rule"] for a in alerts if isinstance(a, dict)] == ["rebalance-needed"]
    assert ntfy.requests == []
    assert _log_rows(capsys) == []


def test_check_without_topic_degraded_but_recorded(
    capsys: pytest.CaptureFixture[str],
    mock_everything: None,
    ntfy: NtfyRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _snapshot(capsys)
    monkeypatch.delenv("DEXPAPRIKA_SECRET_NTFY_TOPIC")
    code, out = run_json(capsys, "alerts", "check")
    assert code == EXIT_DEGRADED
    assert ntfy.requests == []
    rows = _log_rows(capsys)
    assert rows  # firings recorded even with no channel
    assert all(row["delivered"] == 0 for row in rows)
    assert all(row["ntfy_status"] == "no-topic" for row in rows)
    # secrets_present health fails too -> healthcheck-degraded among the firings
    assert "healthcheck-degraded" in {row["rule"] for row in rows}


def test_alerts_test_sends_one_notification(
    capsys: pytest.CaptureFixture[str], mock_everything: None, ntfy: NtfyRecorder
) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(capsys, "alerts", "test")
    assert code == EXIT_OK
    receipt = out["receipt"]
    assert isinstance(receipt, dict)
    assert receipt["id"] == NTFY_RECEIPT["id"]
    assert len(ntfy.requests) == 1
    assert TOPIC not in json.dumps(out)


def test_alerts_log_limit(capsys: pytest.CaptureFixture[str], mock_everything: None) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(capsys, "alerts", "log", "--limit", "5")
    assert code == EXIT_OK
    assert out["alerts"] == []
