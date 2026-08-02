"""Approval loop + instruction validation + ntfy poll (S9 core coverage)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from dexpaprika.alerts.ntfy import NtfyClient
from dexpaprika.config import Settings
from dexpaprika.execute.approval import request_approval
from dexpaprika.execute.instruction import OrderInstruction
from dexpaprika.storage.db import connect
from dexpaprika.storage.migrations import migrate

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
TOPIC = "secret-topic-abc123"


class FakeClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _approval(replies: list[list[str]], *, timeout_minutes: int = 10) -> tuple[str, object]:
    clock = FakeClock()
    published: list[str] = []
    queue = list(replies)

    def poller(_since: int) -> list[str]:
        return queue.pop(0) if queue else []

    decision = request_approval(
        "abc123",
        "set-sl-trigger trigger_price=$1926",
        publisher=lambda _t, body, _p: published.append(body),
        poller=poller,
        clock=clock,
        sleeper=lambda s: clock.advance(s * 60),  # fast-forward
        timeout_minutes=timeout_minutes,
    )
    return published[0], decision


class TestApprovalLoop:
    def test_approve_reply_approves(self) -> None:
        message, decision = _approval([[], ["approve abc123"]])
        assert "approve abc123" in message  # instructions shown to Richard
        assert decision.approved is True  # type: ignore[attr-defined]

    def test_reject_reply_rejects(self) -> None:
        _message, decision = _approval([["reject abc123"]])
        assert decision.approved is False  # type: ignore[attr-defined]

    def test_wrong_id_ignored_then_timeout(self) -> None:
        _message, decision = _approval([["approve ffffff"], ["yes"], ["approve"]])
        assert decision.approved is False  # type: ignore[attr-defined]
        assert "timeout" in decision.reason  # type: ignore[attr-defined]

    def test_case_insensitive_match(self) -> None:
        _message, decision = _approval([["APPROVE ABC123"]])
        assert decision.approved is True  # type: ignore[attr-defined]


class TestInstructionValidation:
    def test_set_sl_requires_price_and_key(self) -> None:
        with pytest.raises(ValueError, match="set-sl-trigger"):
            OrderInstruction(action="set-sl-trigger", trigger_price=Decimal("1926"))

    def test_resize_requires_target(self) -> None:
        with pytest.raises(ValueError, match="resize-short"):
            OrderInstruction(action="resize-short")

    def test_cancel_requires_key(self) -> None:
        with pytest.raises(ValueError, match="cancel-order"):
            OrderInstruction(action="cancel-order")

    def test_summary_restates_parameters(self) -> None:
        instruction = OrderInstruction(
            action="set-sl-trigger", order_key="0xabc", trigger_price=Decimal("1926")
        )
        summary = instruction.summary()
        assert "1926" in summary
        assert "0xabc" in summary
        assert "ETH/USD" in summary


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "t.db")
    migrate(connection)
    connection.execute(
        "INSERT INTO providers (name, base_url, rate_limit, rate_period, has_credits)"
        " VALUES ('ntfy', 'https://ntfy.sh', 30, 'minute', 0)"
    )
    yield connection
    connection.close()


class TestNtfyPoll:
    def _client(self, conn: sqlite3.Connection, handler: httpx.MockTransport) -> NtfyClient:
        return NtfyClient(
            conn,
            settings=Settings.load(),
            client=httpx.Client(transport=handler, base_url="https://ntfy.sh"),
            topic=TOPIC,
            sleeper=lambda _s: None,
        )

    def test_poll_parses_ndjson_messages(self, conn: sqlite3.Connection) -> None:
        lines = "\n".join(
            [
                json.dumps({"event": "open", "id": "x"}),
                json.dumps({"event": "message", "id": "y", "message": "approve abc123"}),
                json.dumps({"event": "keepalive"}),
            ]
        )
        seen: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text=lines)

        messages = self._client(conn, httpx.MockTransport(handle)).poll(1754000000)
        assert messages == ["approve abc123"]
        assert seen[0].url.params["poll"] == "1"

    def test_poll_error_never_contains_topic(self, conn: sqlite3.Connection) -> None:
        handler = httpx.MockTransport(lambda _r: httpx.Response(500, text="boom"))
        with pytest.raises(Exception, match="ntfy") as excinfo:
            self._client(conn, handler).poll(0)
        assert TOPIC not in str(excinfo.value)
        assert TOPIC not in repr(excinfo.value)

    def test_poll_endpoint_label_hides_topic(self, conn: sqlite3.Connection) -> None:
        handler = httpx.MockTransport(lambda _r: httpx.Response(200, text=""))
        self._client(conn, handler).poll(0)
        rows = conn.execute("SELECT endpoint FROM api_call_log").fetchall()
        assert rows
        assert all(TOPIC not in row["endpoint"] for row in rows)
