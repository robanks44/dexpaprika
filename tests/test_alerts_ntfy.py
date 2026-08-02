"""ntfy client — JSON publish, receipt parsing, and topic hygiene."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from dexpaprika.alerts.ntfy import NtfyClient
from dexpaprika.clients.base import TransportError
from dexpaprika.config import Settings
from dexpaprika.storage.db import connect
from dexpaprika.storage.migrations import migrate

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "probes" / "out" / "s8" / "publish_receipt.json").read_text()
)
TOPIC = "secret-topic-abc123"


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


def _client_with(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler, base_url="https://ntfy.sh")


def _make(conn: sqlite3.Connection, handler: httpx.MockTransport) -> NtfyClient:
    return NtfyClient(
        conn,
        settings=Settings.load(),
        client=_client_with(handler),
        topic=TOPIC,
        sleeper=lambda _s: None,
    )


class TestPublish:
    def test_json_publish_receipt(self, conn: sqlite3.Connection) -> None:
        seen: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text=json.dumps(FIXTURE["receipt"]))

        client = _make(conn, httpx.MockTransport(handle))
        receipt = client.publish("Title", "Body", priority="urgent", tags=("warning",))
        assert receipt.id == FIXTURE["receipt"]["id"]
        assert receipt.event == "message"
        # Receipt never carries the topic back to callers.
        assert TOPIC not in receipt.model_dump_json()

        [request] = seen
        body = json.loads(request.content)
        assert request.url.path == "/"  # topic rides the JSON body, NEVER the URL
        assert body["topic"] == TOPIC
        assert body["priority"] == 5
        assert body["tags"] == ["warning"]

    def test_priority_names_map_to_ntfy_levels(self, conn: sqlite3.Connection) -> None:
        priorities: list[int] = []

        def handle(request: httpx.Request) -> httpx.Response:
            priorities.append(json.loads(request.content)["priority"])
            return httpx.Response(200, text=json.dumps(FIXTURE["receipt"]))

        client = _make(conn, httpx.MockTransport(handle))
        for name in ("min", "low", "default", "high", "urgent"):
            client.publish("t", "m", priority=name)
        assert priorities == [1, 2, 3, 4, 5]

    def test_actions_forwarded(self, conn: sqlite3.Connection) -> None:
        bodies: list[dict[str, object]] = []

        def handle(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, text=json.dumps(FIXTURE["receipt"]))

        client = _make(conn, httpx.MockTransport(handle))
        actions = [{"action": "view", "label": "Open GMX", "url": "https://app.gmx.io"}]
        client.publish("t", "m", actions=actions)
        assert bodies[0]["actions"] == actions


class TestTopicHygiene:
    def test_failure_error_never_contains_topic(self, conn: sqlite3.Connection) -> None:
        handler = httpx.MockTransport(lambda _r: httpx.Response(500, text="boom"))
        client = _make(conn, handler)
        with pytest.raises(TransportError) as excinfo:
            client.publish("t", "m")
        assert TOPIC not in str(excinfo.value)
        assert TOPIC not in repr(excinfo.value)

    def test_call_log_endpoint_has_no_topic(self, conn: sqlite3.Connection) -> None:
        handler = httpx.MockTransport(
            lambda _r: httpx.Response(200, text=json.dumps(FIXTURE["receipt"]))
        )
        _make(conn, handler).publish("t", "m")
        rows = conn.execute("SELECT endpoint FROM api_call_log").fetchall()
        assert rows
        for row in rows:
            assert TOPIC not in row["endpoint"]

    def test_client_repr_has_no_topic(self, conn: sqlite3.Connection) -> None:
        handler = httpx.MockTransport(lambda _r: httpx.Response(200, text="{}"))
        client = _make(conn, handler)
        assert TOPIC not in repr(client)
