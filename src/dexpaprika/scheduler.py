"""Container/VPS scheduler (S11) — the scheduling playbook's Option B.

Windows keeps Task Scheduler (S8/RUNBOOK); a container has no external
scheduler, so this persistent process drives the SAME CLI mains in-process.
Playbook knobs on every job: ``max_instances=1`` (never overlap),
``coalesce=True`` (a sleep/lag backlog collapses to ONE catch-up run),
``misfire_grace_time`` (too-stale runs are skipped, never run late).

Each run emits one JSON line with the CLI exit code — container logs stay
exit-code-honest, exactly like schtasks history on Windows.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from dexpaprika.config import Settings

MISFIRE_GRACE_SECONDS = 120


def _cli_main(argv: list[str]) -> int:
    """Late import seam (tests monkeypatch this; avoids a cli<->scheduler cycle)."""
    from dexpaprika.cli import main

    return main(argv)


@dataclass(frozen=True)
class JobSpec:
    """One scheduled CLI invocation."""

    id: str
    argv: list[str]
    trigger: str  # "cron-hourly" | "interval" | "cron-daily"
    minutes: int | None = None  # interval jobs
    hour: int | None = None  # daily cron jobs
    minute: int | None = None


def job_specs(settings: Settings) -> list[JobSpec]:
    """Scheduled jobs: hourly snapshot, minutes-scale alerts monitor, daily backup,
    external watchdog heartbeat (interval), and the daily all-clear digest (S13)."""
    return [
        JobSpec(id="snapshot", argv=["snapshot", "--json"], trigger="cron-hourly", minute=0),
        JobSpec(
            id="alerts-check",
            argv=["alerts", "check", "--json"],
            trigger="interval",
            minutes=settings.scheduler_alerts_minutes,
        ),
        JobSpec(
            id="db-backup",
            argv=["db", "backup", "--json"],
            trigger="cron-daily",
            hour=3,
            minute=10,
        ),
        JobSpec(
            id="watchdog-heartbeat",
            argv=["watchdog", "heartbeat", "--json"],
            trigger="interval",
            minutes=settings.watchdog_heartbeat_minutes,
        ),
        JobSpec(
            id="watchdog-digest",
            argv=["watchdog", "digest", "--json"],
            trigger="cron-daily",
            hour=settings.watchdog_digest_hour,
            minute=0,
        ),
        JobSpec(
            id="strategy-rebalance",
            argv=["strategy", "rebalance", "--arm", "--json"],
            trigger="interval",
            minutes=settings.strategy_rebalance_minutes,
        ),
    ]


def run_job(job_id: str, argv: list[str]) -> None:
    """Run one CLI main; log the outcome as a JSON line; NEVER raise.

    A crashing job must not kill the scheduler process — the failure is
    visible in the log line (and, for alert-path failures, in alerts_log).
    """
    record: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(),
        "job": job_id,
        "argv": argv,
    }
    try:
        record["exit_code"] = _cli_main(argv)
    except Exception as exc:  # scheduler survival is the contract — log, never die
        record["exit_code"] = 1
        record["error"] = f"{type(exc).__name__}: {exc}"
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def _trigger_for(spec: JobSpec) -> CronTrigger | IntervalTrigger:
    if spec.trigger == "interval":
        return IntervalTrigger(minutes=spec.minutes or 5)
    if spec.trigger == "cron-daily":
        return CronTrigger(hour=spec.hour or 0, minute=spec.minute or 0, timezone="UTC")
    return CronTrigger(minute=spec.minute or 0, timezone="UTC")  # cron-hourly


def build_scheduler(settings: Settings) -> BlockingScheduler:
    """Configured (not started) scheduler with the playbook knobs applied."""
    scheduler = BlockingScheduler(timezone="UTC")
    for spec in job_specs(settings):
        scheduler.add_job(
            run_job,
            _trigger_for(spec),
            args=[spec.id, spec.argv],
            id=spec.id,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
        )
    return scheduler
