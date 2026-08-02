"""Scheduler entrypoint (S11) — playbook Option B knobs, offline."""

from __future__ import annotations

import json

import pytest

from dexpaprika.config import Settings
from dexpaprika.scheduler import build_scheduler, job_specs, run_job

EXPECTED_JOBS = {"snapshot", "alerts-check", "db-backup"}


def settings() -> Settings:
    return Settings.load()


class TestJobSpecs:
    def test_three_jobs_with_cli_argv(self) -> None:
        specs = {spec.id: spec for spec in job_specs(settings())}
        assert set(specs) == EXPECTED_JOBS
        assert specs["snapshot"].argv == ["snapshot", "--json"]
        assert specs["alerts-check"].argv == ["alerts", "check", "--json"]
        assert specs["db-backup"].argv == ["db", "backup", "--json"]

    def test_alerts_cadence_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEXPAPRIKA_SCHEDULER_ALERTS_MINUTES", "7")
        specs = {spec.id: spec for spec in job_specs(Settings.load())}
        assert specs["alerts-check"].minutes == 7


class TestBuildScheduler:
    def test_playbook_knobs_on_every_job(self) -> None:
        scheduler = build_scheduler(settings())
        jobs = scheduler.get_jobs()
        assert {job.id for job in jobs} == EXPECTED_JOBS
        for job in jobs:
            assert job.max_instances == 1  # never overlap
            assert job.coalesce is True  # missed backlog -> ONE catch-up run
            assert job.misfire_grace_time == 120  # too-stale runs skip, not run late

    def test_snapshot_hourly_on_the_hour(self) -> None:
        scheduler = build_scheduler(settings())
        trigger = scheduler.get_job("snapshot").trigger
        assert type(trigger).__name__ == "CronTrigger"
        assert "minute='0'" in str(trigger)

    def test_alerts_interval_trigger(self) -> None:
        scheduler = build_scheduler(settings())
        trigger = scheduler.get_job("alerts-check").trigger
        assert type(trigger).__name__ == "IntervalTrigger"


class TestRunJob:
    def test_runs_cli_main_and_logs_exit_code(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[str]] = []

        def fake_main(argv: list[str]) -> int:
            seen.append(argv)
            return 0

        monkeypatch.setattr("dexpaprika.scheduler._cli_main", fake_main)
        run_job("snapshot", ["snapshot", "--json"])
        assert seen == [["snapshot", "--json"]]
        line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert line["job"] == "snapshot"
        assert line["exit_code"] == 0

    def test_nonzero_exit_logged_not_raised(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("dexpaprika.scheduler._cli_main", lambda _argv: 3)
        run_job("alerts-check", ["alerts", "check", "--json"])  # must not raise
        line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert line["exit_code"] == 3

    def test_crash_inside_cli_logged_not_raised(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_argv: list[str]) -> int:
            msg = "kaput"
            raise RuntimeError(msg)

        monkeypatch.setattr("dexpaprika.scheduler._cli_main", boom)
        run_job("db-backup", ["db", "backup", "--json"])  # must not raise
        line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert line["exit_code"] == 1
        assert "kaput" in line["error"]


class TestCli:
    def test_scheduler_jobs_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        from dexpaprika.cli import EXIT_OK, main

        code = main(["scheduler", "jobs", "--json"])
        assert code == EXIT_OK
        out = json.loads(capsys.readouterr().out)
        jobs = out["jobs"]
        assert isinstance(jobs, list)
        by_id = {j["id"]: j for j in jobs}
        assert set(by_id) == EXPECTED_JOBS
        for job in by_id.values():
            assert job["max_instances"] == 1
            assert job["coalesce"] is True
            assert job["misfire_grace_time"] == 120
