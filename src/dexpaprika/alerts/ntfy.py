"""ntfy publisher (reference: ntfy--api-reference.md).

Topic hygiene is the design constraint: the topic is a knowledge-of-name
secret, so it must never reach a URL path, the api_call_log endpoint column,
an exception message, or a repr. JSON publish (POST ``/`` with the topic in
the body) satisfies all four at once — the transport only ever sees the
endpoint label ``publish``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

import httpx
from pydantic import BaseModel

from dexpaprika.clients.base import HttpTransport, Sleeper
from dexpaprika.config import Settings

PROVIDER = "ntfy"

# ntfy numeric priority levels (docs/publish.md).
PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}


class PublishReceipt(BaseModel):
    """Server acknowledgment of one publish — topic deliberately absent."""

    id: str
    time: int
    event: str
    title: str | None = None
    message: str | None = None
    priority: int | None = None
    tags: list[str] | None = None
    expires: int | None = None


class NtfyClient:
    """Publish alerts to the configured ntfy server via the shared transport."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        settings: Settings,
        client: httpx.Client,
        topic: str,
        sleeper: Sleeper | None = None,
    ) -> None:
        self.__topic = topic  # name-mangled: keeps the topic out of naive introspection
        self._transport = HttpTransport(
            base_url=settings.ntfy_server,
            provider=PROVIDER,
            conn=conn,
            client=client,
            sleeper=sleeper,
        )

    def publish(
        self,
        title: str,
        message: str,
        *,
        priority: str = "default",
        tags: Sequence[str] = (),
        click: str | None = None,
        actions: list[dict[str, Any]] | None = None,
    ) -> PublishReceipt:
        """Publish one notification; returns the parsed (topic-free) receipt."""
        payload: dict[str, Any] = {
            "topic": self.__topic,
            "title": title,
            "message": message,
            "priority": PRIORITIES[priority],
        }
        if tags:
            payload["tags"] = list(tags)
        if click:
            payload["click"] = click
        if actions:
            payload["actions"] = actions
        raw = self._transport.post_json("/", payload, endpoint_label="publish")
        if not isinstance(raw, dict):  # pragma: no cover — server contract
            raw = {}
        raw.pop("topic", None)  # receipt echoes the topic; strip before it leaves here
        return PublishReceipt.model_validate(raw)

    def __repr__(self) -> str:
        return "NtfyClient(topic=REDACTED)"
