"""Shared HTTP transport for all API clients (ENGINEERING_STANDARDS §2).

Every request: quota-gated (wait_for_slot) → sent with a timeout → logged to
``api_call_log`` (status, latency, correlation id) → response capped at
10 MB and parsed with ``parse_float=Decimal`` so money never rides a float.

Failure policy: 429/5xx/transport errors retry with exponential backoff (+
non-cryptographic jitter); other 4xx fail fast. A per-upstream circuit
breaker opens after N consecutive exhausted calls and half-opens after a
cooldown — a dead upstream costs one fast error, not a retry storm.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from dexpaprika.quota import QuotaTracker

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]


class TransportError(Exception):
    """A request failed in a way the caller should surface, message says how."""


class CircuitOpenError(TransportError):
    """Upstream circuit is open — recent calls all failed; wait out the cooldown."""


class HttpTransport:
    """Quota-gated, Decimal-safe, retrying HTTP transport for one upstream."""

    def __init__(
        self,
        *,
        base_url: str,
        provider: str,
        conn: sqlite3.Connection,
        client: httpx.Client,
        clock: Clock | None = None,
        sleeper: Sleeper | None = None,
        max_attempts: int = 4,
        breaker_threshold: int = 5,
        breaker_cooldown: float = 60.0,
    ) -> None:
        if not base_url.startswith("https://"):
            msg = f"base URL {base_url!r} must use HTTPS (ENGINEERING_STANDARDS §2)"
            raise TransportError(msg)
        self._provider = provider
        self._client = client
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        self._sleeper: Sleeper = sleeper or time.sleep
        self._quota = QuotaTracker(conn, now=self._clock)
        self._max_attempts = max_attempts
        self._breaker_threshold = breaker_threshold
        self._breaker_cooldown = breaker_cooldown
        self._consecutive_failures = 0
        self._opened_at: datetime | None = None

    # ------------------------------ breaker ------------------------------

    def _breaker_gate(self) -> None:
        if self._opened_at is None:
            return
        elapsed = (self._clock() - self._opened_at).total_seconds()
        if elapsed < self._breaker_cooldown:
            remaining = self._breaker_cooldown - elapsed
            msg = (
                f"circuit open for {self._provider!r} after"
                f" {self._consecutive_failures} consecutive failures —"
                f" retry in {remaining:.0f}s"
            )
            raise CircuitOpenError(msg)
        # Cooldown elapsed → half-open: allow one probe attempt through.

    def _on_exhausted(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._breaker_threshold:
            self._opened_at = self._clock()

    def _on_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    # ------------------------------ request ------------------------------

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``path`` and return its JSON with all numbers as int/Decimal."""
        self._breaker_gate()
        correlation_id = uuid.uuid4().hex
        last_error = "no attempt made"
        for attempt in range(self._max_attempts):
            self._quota.wait_for_slot(self._provider, path, sleeper=self._sleeper)
            started = self._clock()
            try:
                response = self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                self._quota.record(self._provider, path, status=None, correlation_id=correlation_id)
                last_error = f"transport error: {exc}"
                self._backoff(attempt)
                continue
            latency_ms = int((self._clock() - started).total_seconds() * 1000)
            self._quota.record(
                self._provider,
                path,
                status=response.status_code,
                latency_ms=latency_ms,
                correlation_id=correlation_id,
            )
            if response.status_code in _RETRYABLE_STATUS:
                last_error = f"HTTP {response.status_code} from {self._provider} {path}"
                self._backoff(attempt)
                continue
            if response.status_code >= 400:
                # Non-retryable client error: not an upstream-health signal.
                msg = (
                    f"HTTP {response.status_code} from {self._provider} {path} —"
                    " check the identifier/parameters (no retry)"
                )
                raise TransportError(msg)
            return self._parse(response, path)
        self._on_exhausted()
        msg = f"{self._provider} {path} failed after {self._max_attempts} attempts: {last_error}"
        raise TransportError(msg)

    def _backoff(self, attempt: int) -> None:
        if attempt + 1 >= self._max_attempts:
            return  # no sleep after the final attempt
        base = 0.5 * (2**attempt)
        # Non-cryptographic jitter: schedule spreading only, no security use.
        jitter = random.uniform(0, base / 4)  # noqa: S311 # nosec B311
        self._sleeper(base + jitter)

    def _parse(self, response: httpx.Response, path: str) -> Any:
        if len(response.content) > MAX_RESPONSE_BYTES:
            msg = (
                f"response from {self._provider} {path} too large"
                f" ({len(response.content)} bytes > 10 MB cap)"
            )
            self._on_success()  # upstream is alive; the payload is just absurd
            raise TransportError(msg)
        try:
            payload = json.loads(response.text, parse_float=Decimal)
        except json.JSONDecodeError as exc:
            msg = f"{self._provider} {path} returned invalid JSON: {exc}"
            raise TransportError(msg) from exc
        self._on_success()
        return payload
