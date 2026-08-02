"""Cross-section integration (S10): lifecycle, failure drills, doc integrity."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from dexpaprika.cli import (
    EXIT_DEGRADED,
    EXIT_FAILURE,
    EXIT_OK,
    _check_repo_state,
    build_parser,
    main,
)

REPO = Path(__file__).parent.parent
ROOT = REPO / "probes" / "out"
S5 = json.loads((ROOT / "s5" / "discovery.json").read_text())
S6 = json.loads((ROOT / "s6" / "portfolio.json").read_text())
GMX_FIXTURE = (ROOT / "s4" / "positions_peer_io.json").read_text()
GMX_MARKETS = (ROOT / "s4" / "markets.json").read_text()
PAPRIKA_NETWORKS = (ROOT / "s3" / "networks.json").read_text()
NTFY_RECEIPT = json.loads((ROOT / "s8" / "publish_receipt.json").read_text())["receipt"]
WALLET = S5["wallet"]
RAW = {k.lower(): v for k, v in {**S5["raw_calls"], **S6["raw_calls"]}.items()}
ZERO_WORD = "0x" + "0" * 64


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEXPAPRIKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")
    monkeypatch.setenv("DEXPAPRIKA_SECRET_NTFY_TOPIC", "test-topic")
    monkeypatch.setenv("DEXPAPRIKA_BASE_RPC_URLS", "https://base-rpc.publicnode.com")
    monkeypatch.setenv("DEXPAPRIKA_ARBITRUM_RPC_URLS", "https://arb1.arbitrum.io/rpc")


class MockWorld:
    """One switchboard for every upstream the CLI can reach."""

    def __init__(self) -> None:
        self.rpc_dead = False
        self.clock_offset_s = 0
        self.ntfy_requests: list[httpx.Request] = []

    def rpc_handle(self, request: httpx.Request) -> httpx.Response:
        if self.rpc_dead:
            return httpx.Response(503, text="dead")
        body = json.loads(request.content)
        if body["method"] == "eth_blockNumber":
            result: Any = hex(S5["pin"] + 3)
        elif body["method"] == "eth_getBlockByNumber":
            result = {
                "number": hex(S5["pin"] + 3),
                "timestamp": hex(int(time.time()) + self.clock_offset_s),
            }
        elif body["method"] == "eth_getBalance":
            result = RAW.get(f"native|{body['params'][0].lower()}", "0x0")
        else:
            tx = body["params"][0]
            result = RAW.get(f"{tx['to'].lower()}|{tx['data']}".lower(), ZERO_WORD)
        return httpx.Response(200, text=json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}))

    def gmx_handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/positions"):
            return httpx.Response(200, text=GMX_FIXTURE)
        if request.url.path.endswith("/markets"):
            return httpx.Response(200, text=GMX_MARKETS)
        return httpx.Response(404, text="nope")

    def paprika_handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/networks"):
            return httpx.Response(200, text=PAPRIKA_NETWORKS)
        return httpx.Response(404, text="nope")

    def ntfy_handle(self, request: httpx.Request) -> httpx.Response:
        self.ntfy_requests.append(request)
        return httpx.Response(200, text=json.dumps(NTFY_RECEIPT))

    def client_factory(self, base_url: str) -> httpx.Client:
        if "ntfy" in base_url:
            handler = self.ntfy_handle
        elif "gmxapi" in base_url:
            handler = self.gmx_handle
        elif "dexpaprika" in base_url:
            handler = self.paprika_handle
        else:
            handler = self.rpc_handle
        return httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url=base_url,
            headers={"User-Agent": "dexpaprika/1.0"},
        )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> MockWorld:
    mock = MockWorld()
    monkeypatch.setattr("dexpaprika.cli._http_client_factory", mock.client_factory)
    # Repo state depends on the developer's working tree — pinned in the drill
    # tests below; stubbed here so the lifecycle flow is deterministic.
    monkeypatch.setattr("dexpaprika.cli._check_repo_state", lambda: "ok (stubbed in tests)")
    return mock


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def _db_file(capsys: pytest.CaptureFixture[str]) -> Path:
    code, out = run_json(capsys, "db", "status")
    assert code == EXIT_OK
    return Path(str(out["path"]))


def _bootstrap(capsys: pytest.CaptureFixture[str]) -> None:
    run_json(capsys, "db", "migrate")
    run_json(capsys, "wallets", "add", "--chain", "evm", "--address", WALLET)


class TestColdStartLifecycle:
    def test_full_operator_flow(self, capsys: pytest.CaptureFixture[str], world: MockWorld) -> None:
        """migrate → wallets → snapshot → report → hedge → alerts → healthcheck."""
        _bootstrap(capsys)
        code, out = run_json(capsys, "snapshot")
        assert code == EXIT_OK
        recorded = out["recorded"]
        assert isinstance(recorded, dict)
        assert set(recorded) == {"lp", "hedge", "defi", "holdings"}
        assert recorded["lp"] == 1
        assert recorded["hedge"] == 1

        code, report = run_json(capsys, "report")
        assert code == EXIT_OK
        totals = report["totals_usd"]
        assert isinstance(totals, dict)
        assert float(str(totals["lp_value"])) > 0

        code, hedge = run_json(capsys, "hedge", "status")
        assert code == EXIT_OK
        analysis = hedge["analysis"]
        assert isinstance(analysis, dict)
        assert analysis["quadrant"] in ("Q1", "Q2", "Q3", "Q4")

        code, alerts = run_json(capsys, "alerts", "check")
        assert code == EXIT_OK
        fired = alerts["fired"]
        assert isinstance(fired, list)
        assert [a["rule"] for a in fired if isinstance(a, dict)] == ["rebalance-needed"]
        assert len(world.ntfy_requests) == 1

        code, health = run_json(capsys, "healthcheck")
        assert code == EXIT_OK
        checks = health["checks"]
        assert isinstance(checks, dict)
        assert all(str(v).startswith("ok") for v in checks.values()), checks
        assert "not-implemented" not in {str(v) for v in checks.values()}


class TestHealthcheckCompleteness:
    def test_all_nine_checks_real(
        self, capsys: pytest.CaptureFixture[str], world: MockWorld
    ) -> None:
        _bootstrap(capsys)
        run_json(capsys, "snapshot")
        _code, health = run_json(capsys, "healthcheck")
        checks = health["checks"]
        assert isinstance(checks, dict)
        assert set(checks) == {
            "db_integrity",
            "migrations_current",
            "upstream_reachability",
            "secrets_present",
            "clock_sanity",
            "last_snapshot_age",
            "data_dir_writable",
            "repo_state",
            "operational_state",
        }
        assert "not-implemented" not in {str(v) for v in checks.values()}

    def test_upstream_down_fails_reachability(
        self, capsys: pytest.CaptureFixture[str], world: MockWorld
    ) -> None:
        _bootstrap(capsys)
        run_json(capsys, "snapshot")
        world.rpc_dead = True
        code, health = run_json(capsys, "healthcheck")
        assert code == EXIT_DEGRADED
        checks = health["checks"]
        assert isinstance(checks, dict)
        assert str(checks["upstream_reachability"]).startswith("fail")

    def test_clock_skew_fails_clock_sanity(
        self, capsys: pytest.CaptureFixture[str], world: MockWorld
    ) -> None:
        _bootstrap(capsys)
        run_json(capsys, "snapshot")
        world.clock_offset_s = -600  # chain 10 min behind local clock
        code, health = run_json(capsys, "healthcheck")
        assert code == EXIT_DEGRADED
        checks = health["checks"]
        assert isinstance(checks, dict)
        assert str(checks["clock_sanity"]).startswith("fail")

    def test_missing_snapshots_fail_age_check(
        self, capsys: pytest.CaptureFixture[str], world: MockWorld
    ) -> None:
        _bootstrap(capsys)  # migrated but never snapshotted
        code, health = run_json(capsys, "healthcheck")
        assert code == EXIT_DEGRADED
        checks = health["checks"]
        assert isinstance(checks, dict)
        assert str(checks["last_snapshot_age"]).startswith("fail")
        assert "snapshot" in str(checks["last_snapshot_age"])

    def test_operational_state_reports_execution_disabled(
        self, capsys: pytest.CaptureFixture[str], world: MockWorld
    ) -> None:
        _bootstrap(capsys)
        run_json(capsys, "snapshot")
        _code, health = run_json(capsys, "healthcheck")
        checks = health["checks"]
        assert isinstance(checks, dict)
        state = str(checks["operational_state"])
        assert state.startswith("ok")
        assert "read-only" in state

    def test_exposure_beyond_configured_limit_fails(
        self,
        capsys: pytest.CaptureFixture[str],
        world: MockWorld,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Limits set + live short bigger than the cap = do-not-act state."""
        _bootstrap(capsys)
        run_json(capsys, "snapshot")
        monkeypatch.setenv("DEXPAPRIKA_MAX_POSITION_USD", "1000")  # short is ~$13k
        code, health = run_json(capsys, "healthcheck")
        assert code == EXIT_DEGRADED
        checks = health["checks"]
        assert isinstance(checks, dict)
        assert str(checks["operational_state"]).startswith("fail")


class TestRepoState:
    def _git(self, *args: str, cwd: Path) -> None:
        git = shutil.which("git")
        assert git is not None, "git required for repo_state tests"
        subprocess.run(  # noqa: S603 — fixed argv, test-controlled tmp dir
            [git, *args], cwd=cwd, check=True, capture_output=True
        )

    def test_not_a_checkout_is_ok(self, tmp_path: Path) -> None:
        assert _check_repo_state(tmp_path).startswith("ok")

    def test_clean_checkout_is_ok(self, tmp_path: Path) -> None:
        self._git("init", "-q", cwd=tmp_path)
        (tmp_path / "f.txt").write_text("x")
        self._git("add", ".", cwd=tmp_path)
        self._git(
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "init",
            cwd=tmp_path,
        )
        assert _check_repo_state(tmp_path) == "ok"

    def test_dirty_checkout_fails(self, tmp_path: Path) -> None:
        self._git("init", "-q", cwd=tmp_path)
        (tmp_path / "f.txt").write_text("x")
        result = _check_repo_state(tmp_path)
        assert result.startswith("fail")
        assert "uncommitted" in result


class TestFailureDrills:
    def test_dead_rpc_fails_cleanly_records_nothing(
        self, capsys: pytest.CaptureFixture[str], world: MockWorld
    ) -> None:
        _bootstrap(capsys)
        world.rpc_dead = True
        code, out = run_json(capsys, "snapshot", "--kind", "lp")
        assert code == EXIT_FAILURE
        assert "error" in out
        with closing(sqlite3.connect(_db_file(capsys))) as conn, conn:
            assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0

    def test_stale_recorder_degrades_health_and_alerts(
        self, capsys: pytest.CaptureFixture[str], world: MockWorld
    ) -> None:
        _bootstrap(capsys)
        old = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        with closing(sqlite3.connect(_db_file(capsys))) as conn, conn:
            conn.execute(
                "INSERT INTO snapshots (ts, chain, block_number, kind) VALUES (?, 'base', 1, 'lp')",
                (old,),
            )
        code, health = run_json(capsys, "healthcheck")
        assert code == EXIT_DEGRADED
        checks = health["checks"]
        assert isinstance(checks, dict)
        assert str(checks["last_snapshot_age"]).startswith("fail")

        _code, out = run_json(capsys, "alerts", "check", "--dry-run")
        alerts = out["alerts"]
        assert isinstance(alerts, list)
        assert "snapshot-stale" in {a["rule"] for a in alerts if isinstance(a, dict)}

    def test_quota_budget_exhausted_fails_fast_and_alerts(
        self, capsys: pytest.CaptureFixture[str], world: MockWorld
    ) -> None:
        _bootstrap(capsys)
        run_json(capsys, "quota")  # seeds providers
        with closing(sqlite3.connect(_db_file(capsys))) as conn, conn:
            provider_id = conn.execute(
                "SELECT id FROM providers WHERE name='dexpaprika'"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO api_call_log (ts, provider_id, endpoint, credits)"
                " VALUES (?, ?, 'seed', 200000)",
                (datetime.now(UTC).isoformat(), provider_id),
            )
        code, out = run_json(capsys, "market", "pool", "--network", "base", "--address", "0xp")
        assert code == EXIT_FAILURE
        assert "credit budget exhausted" in str(out["error"])

        _code, dry = run_json(capsys, "alerts", "check", "--dry-run")
        alerts = dry["alerts"]
        assert isinstance(alerts, list)
        assert "quota-critical" in {a["rule"] for a in alerts if isinstance(a, dict)}

    def test_backup_restore_drill(
        self, capsys: pytest.CaptureFixture[str], world: MockWorld
    ) -> None:
        _bootstrap(capsys)
        code, _out = run_json(capsys, "db", "backup")
        assert code == EXIT_OK
        db_file = _db_file(capsys)
        db_file.write_bytes(b"garbage" * 100)  # corrupt the live database
        code, out = run_json(capsys, "db", "restore")
        assert code == EXIT_OK
        assert out["restored"] is True
        code, status = run_json(capsys, "db", "status")
        assert code == EXIT_OK
        assert status["integrity"] == "ok"
        code, wallets = run_json(capsys, "wallets", "list")
        assert code == EXIT_OK
        listed = wallets["wallets"]
        assert isinstance(listed, list)
        assert len(listed) == 1  # data survived the round trip


class TestDocIntegrity:
    """Docs are part of the gate: RUNBOOK commands and paths must stay real."""

    RUNBOOK = (REPO / "RUNBOOK.md").read_text(encoding="utf-8")

    def _runbook_commands(self) -> list[str]:
        commands = []
        in_block = False
        joined = self.RUNBOOK.replace("\\\n", " ")  # shell line continuations
        for line in joined.splitlines():
            if line.strip().startswith("```"):
                in_block = not in_block
                continue
            stripped = line.strip()
            if in_block and stripped.startswith("dexpaprika "):
                cleaned = re.sub(r"\[.*?\]", "", stripped).strip()
                cleaned = cleaned.split("#")[0].strip()
                commands.append(cleaned)
        return commands

    def test_every_runbook_command_parses(self) -> None:
        parser = build_parser()
        commands = self._runbook_commands()
        assert commands, "no dexpaprika commands found in RUNBOOK.md"
        for command in commands:
            argv = command.split()[1:]
            try:
                parser.parse_args(argv)
            except SystemExit as exc:  # argparse exits 2 on parse failure
                pytest.fail(f"RUNBOOK command does not parse: {command!r} ({exc.code})")

    def test_every_cli_command_documented(self) -> None:
        parser = build_parser()
        subparsers = next(
            a for a in parser._actions if isinstance(a, __import__("argparse")._SubParsersAction)
        )
        for name in subparsers.choices:
            assert f"dexpaprika {name}" in self.RUNBOOK, f"CLI command undocumented: {name}"

    def test_referenced_repo_paths_exist(self) -> None:
        texts = [self.RUNBOOK]
        for spec in sorted((REPO / "docs" / "specs").glob("*.md")):
            texts.append(spec.read_text(encoding="utf-8"))
        pattern = re.compile(r"(?<![\w-])(?:docs|probes|src|tests)/[\w./-]+")
        missing = {
            match
            for text in texts
            for match in pattern.findall(text)
            if "NNNN" not in match  # migration filename TEMPLATE, not a path
            and not (REPO / match.rstrip("./")).exists()
        }
        assert not missing, f"docs reference nonexistent paths: {sorted(missing)}"
