"""CLI quota command — summaries end-to-end."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dexpaprika.cli import EXIT_FAILURE, EXIT_OK, main


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEXPAPRIKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_quota_requires_migrated_db(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run_json(capsys, "quota")
    assert code == EXIT_FAILURE
    assert "migrate" in str(out["error"])


def test_quota_lists_seeded_providers(capsys: pytest.CaptureFixture[str]) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(capsys, "quota")
    assert code == EXIT_OK
    providers = out["providers"]
    assert isinstance(providers, list)
    names = {p["provider"] for p in providers if isinstance(p, dict)}
    assert {"dexpaprika", "gmx", "coinstats"} <= names


def test_quota_single_provider_filter(capsys: pytest.CaptureFixture[str]) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(capsys, "quota", "--provider", "dexpaprika")
    assert code == EXIT_OK
    providers = out["providers"]
    assert isinstance(providers, list)
    assert len(providers) == 1


def test_quota_unknown_provider_fails(capsys: pytest.CaptureFixture[str]) -> None:
    run_json(capsys, "db", "migrate")
    code, out = run_json(capsys, "quota", "--provider", "nope")
    assert code == EXIT_FAILURE
    assert "error" in out
