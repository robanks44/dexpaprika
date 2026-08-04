"""External dead-man's-switch heartbeat (S13).

Pings an OFF-machine switch (healthchecks.io-style) while the recorder is
healthy: a healthy machine pings ``ok`` (switch stays green), a stalled-but-alive
machine pings ``fail`` (switch trips loudly), and a DEAD machine sends nothing
(switch trips on silence). The switch — not this process — raises the external
alert, which is the whole point: a dead machine cannot alert itself.

Secret hygiene: the ping URL carries a secret token (its UUID path). It never
reaches logs or exception text — errors are redacted (same discipline as the
ntfy topic). Ping responses are plain text (``OK``), so this uses a direct,
injectable httpx GET rather than the JSON transport.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from dexpaprika.config import Settings
from dexpaprika.secrets import resolve_provider

ClientFactory = Callable[[], httpx.Client]

_STATE_SUFFIX = {"ok": "", "fail": "/fail", "start": "/start"}


class PingResult(BaseModel):
    configured: bool
    sent: bool
    state: str
    status_code: int | None = None
    error: str | None = None


class HealthVerdict(BaseModel):
    ok: bool
    newest_ts: str | None
    age_seconds: float | None
    reason: str


class HeartbeatResult(BaseModel):
    verdict: HealthVerdict
    ping: PingResult


def _default_client() -> httpx.Client:
    return httpx.Client(timeout=10.0, headers={"User-Agent": "dexpaprika/1.0"})


def _redact(text: str, url: str) -> str:
    """Strip the secret URL (and its token path) out of any message."""
    token = urlparse(url).path.strip("/")
    out = text.replace(url, "REDACTED")
    if token:
        out = out.replace(token, "REDACTED")
    return out


def ping(
    settings: Settings,
    *,
    state: str = "ok",
    client_factory: ClientFactory | None = None,
) -> PingResult:
    """Ping the configured dead-man's switch. Unset URL → configured=False (honest no-op)."""
    if state not in _STATE_SUFFIX:
        msg = f"invalid heartbeat state {state!r}"
        raise ValueError(msg)
    url = resolve_provider(settings).get("heartbeat_url")
    if not url:
        return PingResult(configured=False, sent=False, state=state)
    if not url.startswith("https://"):
        return PingResult(
            configured=True, sent=False, state=state, error="heartbeat_url must be https://"
        )
    target = url.rstrip("/") + _STATE_SUFFIX[state]
    factory = client_factory or _default_client
    try:
        client = factory()
        try:
            resp = client.get(target)
        finally:
            client.close()
    except Exception as exc:  # network failure — redact the token, never leak it
        return PingResult(configured=True, sent=False, state=state, error=_redact(str(exc), url))
    return PingResult(configured=True, sent=True, state=state, status_code=resp.status_code)


def assess_health(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    now: datetime,
    max_age_s: float | None = None,
) -> HealthVerdict:
    """Is the recorder fresh? Reads the newest snapshot ts (no network)."""
    limit = max_age_s if max_age_s is not None else settings.watchdog_stale_minutes * 60
    row = conn.execute("SELECT MAX(ts) AS ts FROM snapshots").fetchone()
    newest = row["ts"] if row else None
    if not newest:
        return HealthVerdict(ok=False, newest_ts=None, age_seconds=None, reason="no snapshots")
    age = (now - datetime.fromisoformat(newest)).total_seconds()
    if age > limit:
        return HealthVerdict(
            ok=False,
            newest_ts=newest,
            age_seconds=age,
            reason=f"stale: newest snapshot {age / 60:.1f}m old (> {limit / 60:.0f}m)",
        )
    return HealthVerdict(ok=True, newest_ts=newest, age_seconds=age, reason="fresh")


def run_heartbeat(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    now: datetime,
    client_factory: ClientFactory | None = None,
) -> HeartbeatResult:
    """Assess freshness, then ping ok (healthy) or fail (stale). Scheduler entrypoint."""
    verdict = assess_health(conn, settings, now=now)
    state = "ok" if verdict.ok else "fail"
    result = ping(settings, state=state, client_factory=client_factory)
    return HeartbeatResult(verdict=verdict, ping=result)
