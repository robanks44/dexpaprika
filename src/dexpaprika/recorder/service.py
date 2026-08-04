"""Recorder service — the long-running loop around ``run_cycle`` (S12a).

Liveness only: correctness never requires this daemon (ENGINEERING_STANDARDS §6).
A series of scheduled ``recorder cycle`` calls produces the same rows as ``run``.

Design:
- each source has its own cadence; a tick runs only the sources that are due;
- a failed source keeps its previous (now-stale) stamp, is flagged not-ok, and
  is retried on capped exponential backoff — one bad source never stops the loop;
- ``clock`` and ``sleep`` are injected, so tests advance time deterministically
  with zero real waiting;
- ``request_stop`` / ``max_cycles`` end the loop cleanly between cycles.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from dexpaprika.config import Settings
from dexpaprika.recorder.cycle import CycleResult, RecorderClients, SourceStamp, run_cycle

Clock = Callable[[], datetime]
Sleep = Callable[[float], None]
CycleFn = Callable[..., CycleResult]


@dataclass
class ServiceStatus:
    running: bool
    cycles: int
    sources: dict[str, SourceStamp] = field(default_factory=dict)
    staleness_seconds: dict[str, float | None] = field(default_factory=dict)
    last_cycle_ts: str | None = None


class RecorderService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        *,
        kinds: Sequence[str],
        wallets: Sequence[str],
        btc_wallets: Sequence[str] = (),
        intervals: Mapping[str, float],
        clock: Clock,
        sleep: Sleep,
        cycle_fn: CycleFn = run_cycle,
        clients: RecorderClients | None = None,
        poll_interval: float | None = None,
        base_backoff: float = 5.0,
        max_backoff: float = 300.0,
    ) -> None:
        self._conn = conn
        self._settings = settings
        self._kinds = list(kinds)
        self._wallets = list(wallets)
        self._btc = list(btc_wallets)
        self._intervals = {k: float(intervals.get(k, 0.0)) for k in self._kinds}
        self._clock = clock
        self._sleep = sleep
        self._cycle_fn = cycle_fn
        self._clients = clients
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        positive = [v for v in self._intervals.values() if v > 0]
        if poll_interval is not None:
            self._poll = poll_interval
        else:
            self._poll = min(positive) if positive else 1.0
        self._stop = False
        self._due_at: dict[str, datetime | None] = dict.fromkeys(self._kinds)
        self._fail_streak: dict[str, int] = dict.fromkeys(self._kinds, 0)
        self._stamps: dict[str, SourceStamp] = {}
        self._cycles = 0
        self._now: datetime | None = None
        self._last_cycle_ts: str | None = None

    def request_stop(self) -> None:
        """Ask the loop to finish after the current tick (cooperative)."""
        self._stop = True

    def _due(self, kind: str, now: datetime) -> bool:
        at = self._due_at[kind]
        return at is None or now >= at

    def _apply(self, result: CycleResult, now: datetime) -> None:
        for kind, stamp in result.sources.items():
            if stamp.ok:
                self._stamps[kind] = stamp
                self._fail_streak[kind] = 0
                self._due_at[kind] = now + timedelta(seconds=self._intervals[kind])
            else:
                # Honest staleness (spec §Behavioural rules): a failed source KEEPS
                # its previous last-good ts/block and is flagged not-ok — never
                # re-stamped fresh. Staleness keeps growing from the last good data.
                prev = self._stamps.get(kind)
                if prev is not None:
                    self._stamps[kind] = SourceStamp(
                        ok=False, ts=prev.ts, block=prev.block, error=stamp.error
                    )
                else:
                    self._stamps[kind] = stamp  # never succeeded — nothing good to keep
                self._fail_streak[kind] += 1
                backoff = min(
                    self._max_backoff, self._base_backoff * (2 ** (self._fail_streak[kind] - 1))
                )
                self._due_at[kind] = now + timedelta(seconds=backoff)

    def run(self, *, max_cycles: int | None = None) -> ServiceStatus:
        while not self._stop and (max_cycles is None or self._cycles < max_cycles):
            now = self._clock()
            self._now = now
            due = [k for k in self._kinds if self._due(k, now)]
            if due:
                result = self._cycle_fn(
                    self._conn,
                    self._settings,
                    kinds=due,
                    wallets=self._wallets,
                    btc_wallets=self._btc,
                    now=now,
                    clients=self._clients,
                )
                self._apply(result, now)
                self._last_cycle_ts = result.ts
            self._cycles += 1
            if not self._stop and (max_cycles is None or self._cycles < max_cycles):
                self._sleep(self._poll)
        return self.status()

    def status(self, *, now: datetime | None = None) -> ServiceStatus:
        ref = now or self._now or (self._clock() if not self._stamps else None)
        staleness: dict[str, float | None] = {}
        for kind in self._kinds:
            stamp = self._stamps.get(kind)
            if stamp is None or ref is None:
                staleness[kind] = None
            else:
                staleness[kind] = (ref - datetime.fromisoformat(stamp.ts)).total_seconds()
        return ServiceStatus(
            running=not self._stop,
            cycles=self._cycles,
            sources=dict(self._stamps),
            staleness_seconds=staleness,
            last_cycle_ts=self._last_cycle_ts,
        )
