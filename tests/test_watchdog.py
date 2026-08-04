"""S13 — external watchdog heartbeat + daily digest (offline)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from dexpaprika.config import Settings
from dexpaprika.storage.db import connect
from dexpaprika.storage.migrations import migrate
from dexpaprika.watchdog import assess_health, build_digest, ping, run_heartbeat, send_digest

T0 = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
HB_URL = "https://hc-ping.com/deadbeef-token"

LP_STATE: dict[str, Any] = {
    "tick_lower": -202000,
    "tick_upper": -200000,
    "liquidity": 3987414535131380,
    "price_usd": "1854.05",
    "in_range": True,
}
PERP_STATE: dict[str, Any] = {
    "size_tokens": "1.0",
    "entry_price": "1900.0",
    "mark_price": "1854.05",
    "liquidation_price": "2100.0",
    "collateral_usd": "1000.0",
    "leverage": "1.85",
    "pnl": "46.0",
    "pending_funding_fees_usd": "1.0",
    "stop_loss_triggers": ["2050.0"],  # far above price 1854 (~10%) → not a near-SL concern
    "stop_loss_orders": [{"trigger": "2050.0", "size_usd": None, "is_full_close": True}],
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")
    monkeypatch.delenv("DEXPAPRIKA_SECRET_HEARTBEAT_URL", raising=False)
    monkeypatch.delenv("DEXPAPRIKA_SECRET_NTFY_TOPIC", raising=False)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "wd.db")
    migrate(c)
    from dexpaprika.quota import QuotaTracker

    QuotaTracker(c).ensure_providers()
    return c


def _settings() -> Settings:
    return Settings.load()


def _add(conn: sqlite3.Connection, kind: str, state: dict[str, Any], ts: datetime) -> None:
    venue, chain = ("aerodrome", "base") if kind == "lp" else ("gmx", "arbitrum")
    conn.execute(
        "INSERT INTO positions (wallet_ref, venue, chain, kind, external_id, group_tag, opened_at)"
        " VALUES ('0xC155', ?, ?, ?, ?, 'lp_hedge', ?)",
        (venue, chain, kind, f"{kind}:1", ts.isoformat()),
    )
    pid = conn.execute("SELECT id FROM positions WHERE kind=?", (kind,)).fetchone()["id"]
    conn.execute(
        "INSERT INTO position_events (position_id, ts, type, delta_json, state_json)"
        " VALUES (?, ?, 'observed', '{}', ?)",
        (pid, ts.isoformat(), json.dumps(state)),
    )
    snap_kind = "hedge" if kind == "perp" else kind
    conn.execute(
        "INSERT INTO snapshots (ts, chain, block_number, kind) VALUES (?, ?, 1, ?)",
        (ts.isoformat(), chain, snap_kind),
    )


def _seed(conn: sqlite3.Connection, *, ts: datetime, in_range: bool = True) -> None:
    lp = dict(LP_STATE)
    lp["in_range"] = in_range
    _add(conn, "lp", lp, ts)
    _add(conn, "perp", PERP_STATE, ts)


def _capturing_factory(paths: list[str], *, status: int = 200) -> Any:
    def factory() -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(status, text="OK")

        return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://hc-ping.com")

    return factory


# --------------------------- 1-2. ping ---------------------------


def test_ping_unconfigured_is_honest_noop() -> None:
    result = ping(_settings(), state="ok")
    assert result.configured is False
    assert result.sent is False


def test_ping_configured_hits_correct_path_per_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEXPAPRIKA_SECRET_HEARTBEAT_URL", HB_URL)
    for state, suffix in [("ok", ""), ("fail", "/fail"), ("start", "/start")]:
        paths: list[str] = []
        result = ping(_settings(), state=state, client_factory=_capturing_factory(paths))
        assert result.configured and result.sent and result.status_code == 200
        assert paths == [f"/deadbeef-token{suffix}"]


def test_ping_error_redacts_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEXPAPRIKA_SECRET_HEARTBEAT_URL", HB_URL)

    def boom_factory() -> httpx.Client:
        def handler(_r: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"failed connecting to {HB_URL}")

        return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://hc-ping.com")

    result = ping(_settings(), state="ok", client_factory=boom_factory)
    assert result.sent is False
    assert "deadbeef-token" not in (result.error or "")
    assert "REDACTED" in (result.error or "")


# --------------------------- 3. assess_health ---------------------------


def test_assess_health_fresh_stale_empty(conn: sqlite3.Connection) -> None:
    # Threshold reuses snapshot_staleness_minutes (default 90) — fresh well within an
    # hourly cadence, stale only well past it (no false alarms against hourly snapshots).
    assert assess_health(conn, _settings(), now=T0).ok is False  # empty → not ok
    _seed(conn, ts=T0)
    assert assess_health(conn, _settings(), now=T0 + timedelta(minutes=45)).ok is True
    stale = assess_health(conn, _settings(), now=T0 + timedelta(hours=2))
    assert stale.ok is False and "stale" in stale.reason


# --------------------------- 4. run_heartbeat ---------------------------


def test_run_heartbeat_pings_ok_when_fresh_fail_when_stale(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEXPAPRIKA_SECRET_HEARTBEAT_URL", HB_URL)
    _seed(conn, ts=T0)
    ok_paths: list[str] = []
    r_ok = run_heartbeat(
        conn,
        _settings(),
        now=T0 + timedelta(minutes=45),
        client_factory=_capturing_factory(ok_paths),
    )
    assert r_ok.verdict.ok and r_ok.ping.state == "ok" and ok_paths == ["/deadbeef-token"]
    fail_paths: list[str] = []
    r_fail = run_heartbeat(
        conn,
        _settings(),
        now=T0 + timedelta(hours=2),
        client_factory=_capturing_factory(fail_paths),
    )
    assert r_fail.verdict.ok is False and r_fail.ping.state == "fail"
    assert fail_paths == ["/deadbeef-token/fail"]


def test_hourly_cadence_does_not_false_alarm(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression pin (fresh-agent S13): with hourly snapshots, a 55-min-old snapshot
    # (typical just before the next hourly write) must NOT trip the switch or read
    # "attention" — the watchdog stale threshold reuses snapshot_staleness_minutes (90).
    monkeypatch.setenv("DEXPAPRIKA_SECRET_HEARTBEAT_URL", HB_URL)
    _seed(conn, ts=T0, in_range=True)
    near_next = T0 + timedelta(minutes=55)
    assert assess_health(conn, _settings(), now=near_next).ok is True
    paths: list[str] = []
    r = run_heartbeat(conn, _settings(), now=near_next, client_factory=_capturing_factory(paths))
    assert r.ping.state == "ok" and paths == ["/deadbeef-token"]  # ok, not /fail
    assert build_digest(conn, _settings(), now=near_next).all_ok is True  # green, not attention


# --------------------------- 5. build_digest ---------------------------


def test_build_digest_all_clear_when_healthy(conn: sqlite3.Connection) -> None:
    _seed(conn, ts=T0, in_range=True)
    # 45 min old is fresh under the 90-min stale threshold — matches an hourly cadence.
    d = build_digest(conn, _settings(), now=T0 + timedelta(minutes=45))
    assert d.all_ok is True
    assert "all clear" in d.title
    assert "in range" in d.message and "coverage" in d.message and "dist to SL" in d.message
    assert d.concerns == []


def test_build_digest_flags_out_of_range_and_stale(conn: sqlite3.Connection) -> None:
    _seed(conn, ts=T0, in_range=False)
    # 2h later → both sources stale (> 90m) AND LP out of range
    d = build_digest(conn, _settings(), now=T0 + timedelta(hours=2))
    assert d.all_ok is False
    assert "attention" in d.title
    assert any("out of range" in c for c in d.concerns)
    assert any("stale" in c for c in d.concerns)


def test_build_digest_never_green_over_missing_data(conn: sqlite3.Connection) -> None:
    d = build_digest(conn, _settings(), now=T0)  # empty DB
    assert d.all_ok is False
    assert d.concerns  # explicitly lists what's missing, never a fabricated all-clear


# --------------------------- 6. send_digest ---------------------------


def test_send_digest_unconfigured_topic_surfaced(conn: sqlite3.Connection) -> None:
    _seed(conn, ts=T0)
    result = send_digest(conn, _settings(), now=T0 + timedelta(minutes=5))
    assert result.sent is False
    assert result.reason is not None and "ntfy_topic" in result.reason


def test_send_digest_delivers_via_ntfy(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEXPAPRIKA_SECRET_NTFY_TOPIC", "uig-test-topic")
    _seed(conn, ts=T0)
    published: list[dict[str, Any]] = []

    def factory(base_url: str) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            published.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "x", "time": 1, "event": "message"})

        return httpx.Client(transport=httpx.MockTransport(handler), base_url=base_url)

    result = send_digest(conn, _settings(), now=T0 + timedelta(minutes=5), client_factory=factory)
    assert result.sent is True
    assert len(published) == 1
    assert published[0]["topic"] == "uig-test-topic"  # topic only in body, per hygiene
    assert published[0]["title"].startswith("dexpaprika")


# --------------------------- 7. CLI ---------------------------


@pytest.fixture
def _cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEXPAPRIKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEXPAPRIKA_SECRET_BACKEND", "env")


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    from dexpaprika.cli import main

    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_cli_watchdog_status_and_heartbeat_and_digest(
    capsys: pytest.CaptureFixture[str], _cli_env: None
) -> None:
    from dexpaprika.cli import EXIT_DEGRADED, EXIT_OK

    _cli(capsys, "db", "migrate")
    # status: no heartbeat url, empty DB → health not ok, configured False
    code, out = _cli(capsys, "watchdog", "status")
    assert code == EXIT_OK
    assert out["heartbeat_url_configured"] is False
    assert out["health"]["ok"] is False
    # heartbeat auto with no url → DEGRADED (configured False), honest payload
    code, out = _cli(capsys, "watchdog", "heartbeat")
    assert code == EXIT_DEGRADED
    assert out["ping"]["configured"] is False
    # digest dry-run → prints, sends nothing
    code, out = _cli(capsys, "watchdog", "digest", "--dry-run")
    assert code == EXIT_OK
    assert out["dry_run"] is True and out["digest"]["all_ok"] is False
