"""Config-driven, per-upstream quota accounting (REFERENCE_INDEX §3b).

Every external client (S3+) calls ``check`` → ``record`` (or ``wait_for_slot``)
through one tracker. State lives in the shared database (`providers`,
`provider_endpoint_costs`, `api_call_log`), so limits are enforced per
UPSTREAM across any number of client instances — the §3b requirement.

Two windowing semantics, selected per provider via ``config_json``:
- ``calls`` (default): N requests per period (DexPaprika 30/min, Etherscan 5/s).
- ``credits``: summed endpoint weight per period (Hyperliquid 1200 weight/min).

Monthly credit budgets (UTC calendar month) are separate from rate windows:
CoinStats' 400-credit DeFi call is cheap against its per-minute rate but
expensive against a monthly plan. Denials happen BEFORE a limit would be
exceeded, never after.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from importlib import resources
from typing import Any

_PERIOD_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}

Clock = Callable[[], datetime]


class QuotaError(Exception):
    """Configuration-level quota failure (e.g. unknown provider)."""


class QuotaExceededError(QuotaError):
    """A budget that waiting cannot fix (monthly credits) is exhausted."""


@dataclass(frozen=True)
class QuotaVerdict:
    allowed: bool
    wait_seconds: float = 0.0
    reason: str | None = None


def _seed_data() -> list[dict[str, Any]]:
    raw = resources.files("dexpaprika.quota").joinpath("providers.json").read_text("utf-8")
    providers: list[dict[str, Any]] = json.loads(raw)["providers"]
    return providers


class QuotaTracker:
    """Rate + credit accounting over the shared database."""

    def __init__(self, conn: sqlite3.Connection, now: Clock | None = None) -> None:
        self._conn = conn
        self._now: Clock = now or (lambda: datetime.now(UTC))

    # ------------------------------ config ------------------------------

    def ensure_providers(self) -> None:
        """Idempotently upsert the packaged provider seed into the config tables."""
        for provider in _seed_data():
            config = provider.get("config")
            self._conn.execute(
                "INSERT INTO providers"
                " (name, base_url, rate_limit, rate_period, has_credits, credit_limit,"
                "  free_tier, config_json)"
                " VALUES (:name, :base_url, :rate_limit, :rate_period, :has_credits,"
                "         :credit_limit, :free_tier, :config_json)"
                " ON CONFLICT(name) DO UPDATE SET"
                "  base_url=excluded.base_url, rate_limit=excluded.rate_limit,"
                "  rate_period=excluded.rate_period, has_credits=excluded.has_credits,"
                "  credit_limit=excluded.credit_limit, free_tier=excluded.free_tier,"
                "  config_json=excluded.config_json",
                {
                    "name": provider["name"],
                    "base_url": provider["base_url"],
                    "rate_limit": provider["rate_limit"],
                    "rate_period": provider["rate_period"],
                    "has_credits": 1 if provider["has_credits"] else 0,
                    "credit_limit": provider["credit_limit"],
                    "free_tier": 1 if provider.get("free_tier", True) else 0,
                    "config_json": json.dumps(config) if config else None,
                },
            )
            provider_id = self._provider_row(provider["name"])["id"]
            for pattern, credits in provider.get("endpoint_costs", {}).items():
                self._conn.execute(
                    "INSERT INTO provider_endpoint_costs (provider_id, endpoint_pattern, credits)"
                    " VALUES (?, ?, ?)"
                    " ON CONFLICT(provider_id, endpoint_pattern)"
                    " DO UPDATE SET credits=excluded.credits",
                    (provider_id, pattern, credits),
                )

    def _provider_row(self, provider: str) -> sqlite3.Row:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM providers WHERE name = ?", (provider,)
        ).fetchone()
        if row is None:
            msg = f"unknown provider {provider!r} — register it in the providers table first"
            raise QuotaError(msg)
        return row

    def endpoint_credits(self, provider: str, endpoint: str) -> int:
        """Weight for this endpoint: longest matching fnmatch pattern wins; default 1."""
        provider_id = self._provider_row(provider)["id"]
        rows = self._conn.execute(
            "SELECT endpoint_pattern, credits FROM provider_endpoint_costs WHERE provider_id = ?",
            (provider_id,),
        ).fetchall()
        matches = [
            (len(row["endpoint_pattern"]), int(row["credits"]))
            for row in rows
            if fnmatchcase(endpoint, row["endpoint_pattern"])
        ]
        if not matches:
            return 1
        return max(matches)[1]

    # ------------------------------ accounting ------------------------------

    def _window_rows(self, provider_id: int, window_start: datetime) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT ts, credits FROM api_call_log WHERE provider_id = ? AND ts >= ? ORDER BY ts",
            (provider_id, window_start.isoformat()),
        ).fetchall()

    def _month_credits(self, provider_id: int, now: datetime) -> int:
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        row = self._conn.execute(
            "SELECT COALESCE(SUM(credits), 0) AS total FROM api_call_log"
            " WHERE provider_id = ? AND ts >= ?",
            (provider_id, month_start.isoformat()),
        ).fetchone()
        return int(row["total"])

    def check(self, provider: str, endpoint: str) -> QuotaVerdict:
        """Would one more call to ``endpoint`` stay inside every limit?"""
        row = self._provider_row(provider)
        now = self._now()
        credits = self.endpoint_credits(provider, endpoint)
        period = _PERIOD_SECONDS[row["rate_period"]]
        window_mode = "calls"
        if row["config_json"]:
            window_mode = json.loads(row["config_json"]).get("window", "calls")
        window_rows = self._window_rows(row["id"], now - timedelta(seconds=period))

        incoming = credits if window_mode == "credits" else 1
        used = (
            sum(int(r["credits"]) for r in window_rows)
            if window_mode == "credits"
            else len(window_rows)
        )
        if used + incoming > row["rate_limit"]:
            oldest = datetime.fromisoformat(window_rows[0]["ts"]) if window_rows else now
            wait = max((oldest + timedelta(seconds=period) - now).total_seconds(), 0.0)
            return QuotaVerdict(allowed=False, wait_seconds=wait, reason="rate-limit")

        if row["credit_limit"] is not None:
            month = self._month_credits(row["id"], now)
            if month + credits > int(row["credit_limit"]):
                return QuotaVerdict(allowed=False, wait_seconds=0.0, reason="credit-budget")
        return QuotaVerdict(allowed=True)

    def record(
        self,
        provider: str,
        endpoint: str,
        *,
        status: int | None = None,
        latency_ms: int | None = None,
        correlation_id: str | None = None,
    ) -> None:
        """Append the call to the universal log with its resolved credit weight."""
        row = self._provider_row(provider)
        self._conn.execute(
            "INSERT INTO api_call_log (ts, provider_id, endpoint, credits, status,"
            " latency_ms, correlation_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self._now().isoformat(),
                row["id"],
                endpoint,
                self.endpoint_credits(provider, endpoint),
                status,
                latency_ms,
                correlation_id,
            ),
        )

    def wait_for_slot(
        self,
        provider: str,
        endpoint: str,
        sleeper: Callable[[float], None],
    ) -> None:
        """Block (via ``sleeper``) until a rate slot frees; raise on credit budgets."""
        while True:
            verdict = self.check(provider, endpoint)
            if verdict.allowed:
                return
            if verdict.reason == "credit-budget":
                msg = (
                    f"credit budget exhausted for {provider!r} — waiting will not help"
                    " until next month; raise the budget or stop calling"
                )
                raise QuotaExceededError(msg)
            sleeper(max(verdict.wait_seconds, 0.05))

    # ------------------------------ reporting ------------------------------

    def summary(self, provider: str) -> dict[str, Any]:
        row = self._provider_row(provider)
        now = self._now()
        period = _PERIOD_SECONDS[row["rate_period"]]
        window_rows = self._window_rows(row["id"], now - timedelta(seconds=period))
        window_mode = "calls"
        if row["config_json"]:
            window_mode = json.loads(row["config_json"]).get("window", "calls")
        window_used = (
            sum(int(r["credits"]) for r in window_rows)
            if window_mode == "credits"
            else len(window_rows)
        )
        month = self._month_credits(row["id"], now)
        if row["credit_limit"]:
            pct = month / int(row["credit_limit"])
        elif row["rate_limit"]:
            pct = window_used / int(row["rate_limit"])
        else:  # pragma: no cover — schema requires rate_limit NOT NULL
            pct = 0.0
        return {
            "provider": row["name"],
            "window_used": window_used,
            "window_limit": int(row["rate_limit"]),
            "rate_period": row["rate_period"],
            "window_mode": window_mode,
            "month_credits": month,
            "credit_limit": row["credit_limit"],
            "pct_used": round(pct, 6),
        }

    def summaries(self) -> list[dict[str, Any]]:
        names = [r["name"] for r in self._conn.execute("SELECT name FROM providers ORDER BY name")]
        return [self.summary(name) for name in names]
