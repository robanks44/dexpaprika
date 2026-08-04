"""CLI entrypoint (ENGINEERING_STANDARDS §0: agent-first, ``--json``, exit codes).

Exit codes: 0 ok, 1 operational failure, 2 usage error, 3 degraded.
``simulate``/``status`` and ``execute`` will be SEPARATE commands (S7/S9).

``healthcheck`` exits 0 only when ALL nine §2 checks pass (S10 completed the
set — reachability and clock sanity need network; the rest are offline).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from dexpaprika import __version__
from dexpaprika.config import Settings
from dexpaprika.secrets import resolve_provider
from dexpaprika.storage.backup import (
    BackupError,
    create_backup,
    latest_backup,
    restore_backup,
)
from dexpaprika.storage.db import connect, db_path
from dexpaprika.storage.migrations import MigrationError, migrate, pending
from dexpaprika.wallets.registry import RegistryError, Wallet, WalletRegistry
from dexpaprika.wallets.validation import AddressValidationError

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_DEGRADED = 3

_HEALTHCHECKS = (
    "db_integrity",
    "migrations_current",
    "upstream_reachability",
    "secrets_present",
    "clock_sanity",
    "last_snapshot_age",
    "data_dir_writable",
    "repo_state",
    "operational_state",  # dry-run vs armed, kill-switch, exposure vs limits
)


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    else:
        for key, value in payload.items():
            sys.stdout.write(f"{key}: {value}\n")


def _wallet_payload(wallet: Wallet) -> dict[str, Any]:
    return wallet.model_dump()


def _registry(settings: Settings) -> WalletRegistry:
    return WalletRegistry(settings.data_dir / "wallets.json")


# ------------------------------- commands -------------------------------


def _cmd_status(*, as_json: bool) -> int:
    settings = Settings.load()
    try:
        wallets = _registry(settings).list_wallets()
        wallet_summary: dict[str, Any] = {
            "total": len(wallets),
            "included": sum(1 for w in wallets if w.included),
            "by_family": {
                family: sum(1 for w in wallets if w.chain_family == family)
                for family in ("evm", "btc", "solana")
            },
        }
    except RegistryError as exc:
        wallet_summary = {"error": str(exc)}
    _emit(
        {
            "app": "dexpaprika",
            "version": __version__,
            "phase": "building",
            "sections_complete": ["s0", "s1"],
            "config": {
                "data_dir": str(settings.data_dir),
                "log_level": settings.log_level,
                "secret_backend": settings.secret_backend,
            },
            "wallets": wallet_summary,
        },
        as_json=as_json,
    )
    return EXIT_OK


def _check_secrets_present(settings: Settings) -> str:
    provider = resolve_provider(settings)
    if provider.get("ntfy_topic") is None:
        return (
            "fail: secret 'ntfy_topic' not resolvable — store it in the OS keyring "
            "(service 'dexpaprika') or set DEXPAPRIKA_SECRET_NTFY_TOPIC"
        )
    return "ok"


def _check_data_dir_writable(settings: Settings) -> str:
    probe = settings.data_dir / f".write-probe-{uuid.uuid4().hex}"
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return f"fail: cannot write to data dir {settings.data_dir}: {exc}"
    return "ok"


def _check_db_integrity(settings: Settings) -> str:
    path = db_path(settings)
    if not path.exists():
        return "fail: database missing — run `dexpaprika db migrate`"
    conn = connect(path)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            return f"fail: integrity_check reports {integrity!r} — restore from backup"
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            return "fail: foreign_key_check found orphaned rows — restore from backup"
    finally:
        conn.close()
    return "ok"


def _check_migrations_current(settings: Settings) -> str:
    path = db_path(settings)
    if not path.exists():
        return "fail: database missing — run `dexpaprika db migrate`"
    conn = connect(path)
    try:
        outstanding = pending(conn)
    finally:
        conn.close()
    if outstanding:
        return f"fail: {len(outstanding)} pending migration(s) — run `dexpaprika db migrate`"
    return "ok"


def _check_last_snapshot_age(settings: Settings) -> str:
    """Same rule as the S8 `snapshot-stale` alert — one truth, two surfaces."""
    from datetime import UTC, datetime

    path = db_path(settings)
    if not path.exists():
        return "fail: database missing — run `dexpaprika db migrate`"
    conn = connect(path)
    try:
        row = conn.execute("SELECT MAX(ts) AS newest FROM snapshots").fetchone()
    finally:
        conn.close()
    if row["newest"] is None:
        return "fail: no snapshots recorded — run `dexpaprika snapshot`"
    age_minutes = int(
        (datetime.now(UTC) - datetime.fromisoformat(row["newest"])).total_seconds() // 60
    )
    if age_minutes > settings.snapshot_staleness_minutes:
        return (
            f"fail: newest snapshot is {age_minutes} min old"
            f" (threshold {settings.snapshot_staleness_minutes}) — check the scheduled"
            " recorder task, then run `dexpaprika snapshot`"
        )
    return "ok"


def _check_repo_state(root: Path | None = None) -> str:
    """Running uncommitted code is unverified code — flag a dirty checkout."""
    import shutil
    import subprocess  # nosec B404 — read-only `git status`, fixed argv, no shell

    if root is None:
        parents = Path(__file__).resolve().parents
        # src layout: cli.py → dexpaprika → src → repo root (editable install).
        root = parents[2] if len(parents) > 2 else parents[-1]
    if not (root / ".git").exists():
        return "ok (not a git checkout — installed package)"
    git = shutil.which("git")
    if git is None:
        return "ok (git not available — repo state unverified)"
    try:
        result = subprocess.run(  # noqa: S603 # nosec B603 — fixed argv, absolute binary, no shell
            [git, "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"fail: could not verify repo state: {exc}"
    if result.returncode != 0:
        return f"fail: git status failed: {result.stderr.strip()}"
    if result.stdout.strip():
        return "fail: working tree has uncommitted changes — commit or stash before operating"
    return "ok"


def _check_operational_state(settings: Settings) -> str:
    """§2 [v2]: mode, kill-switch, exposure vs limits — the pre-action self-check.

    Reports the REAL execution mode (S9): kill-switch state, whether the
    executor is currently armed, and live exposure vs the configured caps.
    """
    from datetime import UTC, datetime
    from decimal import Decimal

    from dexpaprika.execute.safety import check_armed, check_kill_switch

    path = db_path(settings)
    if not path.exists():
        return "fail: database missing — run `dexpaprika db migrate`"
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT e.state_json FROM positions p"
            " JOIN position_events e ON e.position_id = p.id AND e.type='observed'"
            " WHERE p.kind='perp' AND p.closed_at IS NULL"
            " ORDER BY e.id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    exposure = Decimal(0)
    if row is not None:
        state = json.loads(row["state_json"])
        if state.get("size_usd"):
            exposure = Decimal(str(state["size_usd"]))
    limit = settings.max_position_usd
    if limit > 0 and exposure > limit:
        return (
            f"fail: current short exposure ${exposure} exceeds configured"
            f" max_position_usd ${limit} — do not act; resolve limits first"
        )
    now = datetime.now(UTC)
    kill_tripped = not check_kill_switch(settings).allowed
    armed = check_armed(settings, arm_flag=True, now=now).allowed
    if kill_tripped:
        mode = "kill-switch TRIPPED (all mutating behaviour halted; manual re-arm only)"
    elif armed:
        mode = "ARMED (live execution enabled — an order can be placed with approval)"
    else:
        mode = "dry-run (not armed; live orders require `execute arm` + --arm + approval)"
    limits = "disabled" if limit == 0 else f"max_position_usd=${limit}"
    return f"ok: {mode}; limits {limits}; short exposure ${exposure}"


def _check_network_health(settings: Settings) -> tuple[str, str]:
    """(upstream_reachability, clock_sanity) — one cheap live call per upstream.

    ntfy is deliberately excluded: publishing costs a real notification;
    `dexpaprika alerts test` is its reachability check.
    """
    from datetime import UTC, datetime

    from dexpaprika.chains import ChainRpcError, EvmRpcClient
    from dexpaprika.clients.base import TransportError
    from dexpaprika.clients.dexpaprika import DexPaprikaClient
    from dexpaprika.clients.gmx import GmxClient
    from dexpaprika.quota import QuotaError, QuotaTracker

    path = db_path(settings)
    if not path.exists():
        missing = "fail: database missing (quota accounting) — run `dexpaprika db migrate`"
        return missing, missing
    failures: list[str] = []
    clock = "fail: Base block header unavailable — fix upstream_reachability first"
    conn = connect(path)
    try:
        QuotaTracker(conn).ensure_providers()
        for chain, urls in (
            ("base", settings.base_rpc_urls),
            ("arbitrum", settings.arbitrum_rpc_urls),
        ):
            try:
                client = EvmRpcClient(
                    conn,
                    chain,
                    settings=settings,
                    clients=[_http_client_factory(url) for url in urls],
                )
                if chain == "base":
                    header = client.rpc("eth_getBlockByNumber", ["latest", False])
                    skew = abs(datetime.now(UTC).timestamp() - int(str(header["timestamp"]), 16))
                    clock = (
                        "ok"
                        if skew <= 300
                        else (
                            f"fail: local clock skews {int(skew)}s from Base chain time"
                            " (>300s) — fix the system clock; staleness/cooldown windows"
                            " are unreliable until then"
                        )
                    )
                else:
                    client.block_number()
            except (ChainRpcError, TransportError, QuotaError) as exc:
                failures.append(f"{chain}-rpc: {exc}")
        try:
            GmxClient(
                conn,
                settings=settings,
                clients=[_http_client_factory(peer) for peer in settings.gmx_rest_peers],
            ).get_markets()
        except (TransportError, QuotaError) as exc:
            failures.append(f"gmx: {exc}")
        try:
            DexPaprikaClient(
                conn,
                client=_http_client_factory(settings.dexpaprika_base_url),
                settings=settings,
            ).get_networks()
        except (TransportError, QuotaError, ValueError) as exc:
            failures.append(f"dexpaprika: {exc}")
    finally:
        conn.close()
    reachability = "ok" if not failures else "fail: " + " | ".join(failures)
    return reachability, clock


def _cmd_healthcheck(*, as_json: bool) -> int:
    settings = Settings.load()
    checks = dict.fromkeys(_HEALTHCHECKS, "not-implemented")
    checks["secrets_present"] = _check_secrets_present(settings)
    checks["data_dir_writable"] = _check_data_dir_writable(settings)
    checks["db_integrity"] = _check_db_integrity(settings)
    checks["migrations_current"] = _check_migrations_current(settings)
    checks["last_snapshot_age"] = _check_last_snapshot_age(settings)
    checks["repo_state"] = _check_repo_state()
    checks["operational_state"] = _check_operational_state(settings)
    checks["upstream_reachability"], checks["clock_sanity"] = _check_network_health(settings)
    healthy = all(value.startswith("ok") for value in checks.values())
    _emit(
        {
            "app": "dexpaprika",
            "version": __version__,
            "healthy": healthy,
            "degraded": not healthy,
            "checks": checks,
            "detail": "Exit 0 only when every check passes (ENGINEERING_STANDARDS §2).",
        },
        as_json=as_json,
    )
    return EXIT_OK if healthy else EXIT_DEGRADED


def _cmd_wallets(args: argparse.Namespace, *, as_json: bool) -> int:
    settings = Settings.load()
    registry = _registry(settings)
    try:
        if args.wallets_command == "list":
            payload: dict[str, Any] = {
                "wallets": [_wallet_payload(w) for w in registry.list_wallets()]
            }
        elif args.wallets_command == "add":
            wallet = registry.add(args.chain, args.address, label=args.label)
            payload = {"added": True, "wallet": _wallet_payload(wallet)}
        elif args.wallets_command == "remove":
            wallet = registry.remove(address=args.address, label=args.label)
            payload = {"removed": True, "wallet": _wallet_payload(wallet)}
        else:  # include / exclude
            included = args.wallets_command == "include"
            wallet = registry.set_included(included, address=args.address, label=args.label)
            payload = {"wallet": _wallet_payload(wallet)}
    except (AddressValidationError, RegistryError) as exc:
        _emit({"error": str(exc)}, as_json=as_json)
        return EXIT_FAILURE
    _emit(payload, as_json=as_json)
    return EXIT_OK


def _cmd_db(args: argparse.Namespace, *, as_json: bool) -> int:
    settings = Settings.load()
    path = db_path(settings)
    backups_dir = settings.data_dir / "backups"
    try:
        if args.db_command == "status":
            if not path.exists():
                from dexpaprika.storage.migrations import packaged_migrations

                names = [name for _v, (name, _s) in sorted(packaged_migrations().items())]
                payload: dict[str, Any] = {
                    "path": str(path),
                    "exists": False,
                    "pending": names,
                    "integrity": "n/a — database not created yet",
                }
            else:
                conn = connect(path)
                try:
                    payload = {
                        "path": str(path),
                        "exists": True,
                        "size_bytes": path.stat().st_size,
                        "pending": pending(conn),
                        "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
                    }
                finally:
                    conn.close()
        elif args.db_command == "migrate":
            conn = connect(path)
            try:
                applied = migrate(conn)
            finally:
                conn.close()
            payload = {"applied": applied}
        elif args.db_command == "backup":
            conn = connect(path)
            try:
                backup_file = create_backup(conn, backups_dir)
            finally:
                conn.close()
            payload = {"backup": str(backup_file)}
        else:  # restore
            source = Path(args.source) if args.source else latest_backup(backups_dir)
            if source is None:
                _emit({"error": f"no backups found in {backups_dir}"}, as_json=as_json)
                return EXIT_FAILURE
            restore_backup(source, path)
            payload = {"restored": True, "from": str(source), "to": str(path)}
    except (BackupError, MigrationError, OSError) as exc:
        _emit({"error": str(exc)}, as_json=as_json)
        return EXIT_FAILURE
    _emit(payload, as_json=as_json)
    return EXIT_OK


def _http_client_factory(base_url: str) -> Any:
    """Real HTTP client for CLI-constructed API clients (tests monkeypatch this)."""
    import httpx

    return httpx.Client(
        base_url=base_url,
        timeout=30.0,
        headers={"User-Agent": "dexpaprika/1.0"},  # arb1 403s the default UA
    )


def _cmd_market(args: argparse.Namespace, *, as_json: bool) -> int:
    from dexpaprika.clients.base import TransportError
    from dexpaprika.clients.dexpaprika import DexPaprikaClient
    from dexpaprika.quota import QuotaError, QuotaTracker

    settings = Settings.load()
    path = db_path(settings)
    if not path.exists():
        _emit(
            {"error": "database missing — run `dexpaprika db migrate` first (quota tracking)"},
            as_json=as_json,
        )
        return EXIT_FAILURE
    conn = connect(path)
    try:
        if pending(conn):
            _emit(
                {"error": "schema out of date — run `dexpaprika db migrate` first"},
                as_json=as_json,
            )
            return EXIT_FAILURE
        QuotaTracker(conn).ensure_providers()
        client = DexPaprikaClient(
            conn,
            client=_http_client_factory(settings.dexpaprika_base_url),
            settings=settings,
        )
        if args.market_command == "pool":
            pool = client.get_pool(args.network, args.address)
            payload: dict[str, Any] = {"pool": pool.model_dump(mode="json", exclude={"raw"})}
            if args.record:
                client.record_pool_metrics(pool)
                payload["recorded"] = True
        else:  # ohlcv
            candles = client.get_ohlcv(
                args.network,
                args.address,
                start=args.start,
                interval=args.interval,
                limit=args.limit,
            )
            payload = {"candles": [c.model_dump(mode="json") for c in candles]}
            if args.record:
                payload["recorded"] = client.record_ohlcv(
                    args.network, args.address, args.interval, candles
                )
    except (TransportError, QuotaError, ValueError) as exc:
        _emit({"error": str(exc)}, as_json=as_json)
        return EXIT_FAILURE
    finally:
        conn.close()
    _emit(payload, as_json=as_json)
    return EXIT_OK


def _cmd_hedge(args: argparse.Namespace, *, as_json: bool) -> int:
    from decimal import Decimal

    from dexpaprika.hedge.engine import analyze, simulate
    from dexpaprika.hedge.state import latest_inputs
    from dexpaprika.lp.clmath import price_from_tick

    settings = Settings.load()
    path = db_path(settings)
    if not path.exists():
        _emit(
            {"error": "database missing — run `dexpaprika db migrate` and `snapshot` first"},
            as_json=as_json,
        )
        return EXIT_FAILURE
    conn = connect(path)
    try:
        inputs = latest_inputs(conn)
        if inputs is None:
            _emit(
                {"error": "no recorded LP state — run `dexpaprika snapshot --kind lp` first"},
                as_json=as_json,
            )
            return EXIT_FAILURE
        lp, short, recorded_price = inputs
        if args.hedge_command == "status":
            analysis = analyze(lp, short, recorded_price, settings=settings)
            payload: dict[str, Any] = {"analysis": analysis.model_dump(mode="json")}
        else:  # simulate
            if args.price is not None:
                prices = [Decimal(args.price)]
            else:
                floor = price_from_tick(lp.tick_lower)
                ceiling = price_from_tick(lp.tick_upper)
                n = max(args.curve, 2)
                step = (ceiling - floor) / (n - 1)
                prices = [floor + step * i for i in range(n)]
            entry = short.entry_price if short is not None else recorded_price
            points = simulate(lp, short, prices, entry_price=entry)
            payload = {"points": [p.model_dump(mode="json") for p in points]}
    finally:
        conn.close()
    _emit(payload, as_json=as_json)
    return EXIT_OK


def _resolve_recorder_wallets(
    args: argparse.Namespace, settings: Settings, kinds: list[str]
) -> tuple[list[str], list[str]] | None:
    """(evm_wallets, btc_wallets) for a cycle, or None if the registry is empty.

    An explicit ``--address`` override is EVM-only; BTC holdings come from the
    registry only, and only when ``holdings`` is requested (matches S6 snapshot).
    """
    if args.address:
        return [args.address], []
    registered = _registry(settings).list_wallets()
    wallets = [w.address for w in registered if w.chain_family == "evm" and w.included]
    btc_wallets = (
        [w.address for w in registered if w.chain_family == "btc" and w.included]
        if "holdings" in kinds
        else []
    )
    if not wallets and not btc_wallets:
        return None
    return wallets, btc_wallets


def _cmd_snapshot(args: argparse.Namespace, *, as_json: bool) -> int:
    from datetime import UTC, datetime

    from dexpaprika.quota import QuotaTracker
    from dexpaprika.recorder import build_clients, run_cycle

    settings = Settings.load()
    path = db_path(settings)
    if not path.exists():
        _emit(
            {"error": "database missing — run `dexpaprika db migrate` first"},
            as_json=as_json,
        )
        return EXIT_FAILURE
    kinds = ["lp", "hedge", "defi", "holdings"] if args.kind == "all" else [args.kind]
    resolved = _resolve_recorder_wallets(args, settings, kinds)
    if resolved is None:
        _emit(
            {"error": "no included wallets in the registry — pass --address"},
            as_json=as_json,
        )
        return EXIT_FAILURE
    wallets, btc_wallets = resolved
    conn = connect(path)
    try:
        QuotaTracker(conn).ensure_providers()
        clients = build_clients(
            conn,
            settings,
            kinds=kinds,
            wallets=wallets,
            btc_wallets=btc_wallets,
            client_factory=_http_client_factory,
        )
        result = run_cycle(
            conn,
            settings,
            kinds=kinds,
            wallets=wallets,
            btc_wallets=btc_wallets,
            now=datetime.now(UTC),
            clients=clients,
        )
    finally:
        conn.close()
    # snapshot keeps its fail-hard contract: any failed source → EXIT_FAILURE
    # (the failing source rolled back — nothing partial recorded). Per-source
    # isolation is the recorder SERVICE's behaviour, not the one-shot command.
    if not result.all_ok():
        failed = {k: s.error for k, s in result.sources.items() if not s.ok}
        _emit({"error": f"source(s) failed: {failed}"}, as_json=as_json)
        return EXIT_FAILURE
    _emit(
        {"ts": result.ts, "wallets": result.wallets, "recorded": result.counts},
        as_json=as_json,
    )
    return EXIT_OK


def _cmd_recorder(args: argparse.Namespace, *, as_json: bool) -> int:
    from datetime import UTC, datetime

    from dexpaprika.quota import QuotaTracker
    from dexpaprika.recorder import RecorderService, build_clients, run_cycle

    settings = Settings.load()
    path = db_path(settings)
    if not path.exists():
        _emit(
            {"error": "database missing — run `dexpaprika db migrate` first"},
            as_json=as_json,
        )
        return EXIT_FAILURE

    if args.recorder_command == "status":
        conn = connect(path)
        try:
            # Staleness is measured from the last GOOD (ok=1) heartbeat, while `ok`
            # reflects the latest attempt — a failing source shows its stale
            # last-good time, never a fresh one (honest staleness, ENGINEERING §2).
            rows = conn.execute(
                "SELECT k.kind,"
                " (SELECT ts FROM recorder_heartbeat WHERE kind=k.kind ORDER BY id DESC LIMIT 1)"
                "   AS last_ts,"
                " (SELECT ok FROM recorder_heartbeat WHERE kind=k.kind ORDER BY id DESC LIMIT 1)"
                "   AS last_ok,"
                " (SELECT ts FROM recorder_heartbeat WHERE kind=k.kind AND ok=1"
                "   ORDER BY id DESC LIMIT 1) AS good_ts,"
                " (SELECT block FROM recorder_heartbeat WHERE kind=k.kind AND ok=1"
                "   ORDER BY id DESC LIMIT 1) AS good_block"
                " FROM (SELECT DISTINCT kind FROM recorder_heartbeat) k ORDER BY k.kind"
            ).fetchall()
            now = datetime.now(UTC)
            sources = {
                r["kind"]: {
                    "ok": bool(r["last_ok"]),
                    "last_attempt_ts": r["last_ts"],
                    "last_ok_ts": r["good_ts"],
                    "block": r["good_block"],
                    "staleness_seconds": (
                        (now - datetime.fromisoformat(r["good_ts"])).total_seconds()
                        if r["good_ts"]
                        else None
                    ),
                }
                for r in rows
            }
        finally:
            conn.close()
        _emit({"sources": sources}, as_json=as_json)
        return EXIT_OK

    kinds = ["lp", "hedge", "defi", "holdings"] if args.kind == "all" else [args.kind]
    resolved = _resolve_recorder_wallets(args, settings, kinds)
    if resolved is None:
        _emit(
            {"error": "no included wallets in the registry — pass --address"},
            as_json=as_json,
        )
        return EXIT_FAILURE
    wallets, btc_wallets = resolved
    conn = connect(path)
    try:
        QuotaTracker(conn).ensure_providers()
        clients = build_clients(
            conn,
            settings,
            kinds=kinds,
            wallets=wallets,
            btc_wallets=btc_wallets,
            client_factory=_http_client_factory,
        )
        if args.recorder_command == "cycle":
            result = run_cycle(
                conn,
                settings,
                kinds=kinds,
                wallets=wallets,
                btc_wallets=btc_wallets,
                now=datetime.now(UTC),
                clients=clients,
            )
            _emit(
                {
                    "ts": result.ts,
                    "wallets": result.wallets,
                    "recorded": result.counts,
                    "ok": result.all_ok(),
                },
                as_json=as_json,
            )
            return EXIT_OK

        # recorder run — the service loop (foreground; a scheduler/NSSM wraps it).
        import time

        intervals = {"lp": args.lp_interval, "defi": args.lp_interval, "holdings": args.lp_interval}
        intervals["hedge"] = args.hedge_interval
        service = RecorderService(
            conn,
            settings,
            kinds=kinds,
            wallets=wallets,
            btc_wallets=btc_wallets,
            intervals=intervals,
            clock=lambda: datetime.now(UTC),
            sleep=time.sleep,
            clients=clients,
        )
        status = service.run(max_cycles=args.max_cycles)
    finally:
        conn.close()
    _emit(
        {
            "cycles": status.cycles,
            "sources": {k: {"ok": s.ok, "ts": s.ts} for k, s in status.sources.items()},
        },
        as_json=as_json,
    )
    return EXIT_OK


def _cmd_dashboard(args: argparse.Namespace, *, as_json: bool) -> int:
    settings = Settings.load()
    path = db_path(settings)
    if not path.exists():
        _emit(
            {"error": "database missing — run `dexpaprika db migrate` first"},
            as_json=as_json,
        )
        return EXIT_FAILURE

    if args.dashboard_command == "export":
        from dexpaprika.dashboard.export import render_export

        conn = connect(path)
        try:
            content = render_export(conn, settings)
        finally:
            conn.close()
        out = Path(args.out) if args.out else Path(settings.data_dir) / "dashboard.html"
        out.write_text(content, encoding="utf-8")
        _emit({"written": str(out), "bytes": len(content)}, as_json=as_json)
        return EXIT_OK

    # dashboard serve — read-only server + local DB-watch SSE (blocks).
    import threading

    from dexpaprika.dashboard.server import Broadcaster, DbWatcher, serve

    broadcaster = Broadcaster()

    def _conn_factory() -> Any:
        return connect(path)

    stop_flag = threading.Event()
    watcher = DbWatcher(
        _conn_factory,
        broadcaster,
        sleep=__import__("time").sleep,
        stop=stop_flag.is_set,
        interval=1.0,
    )
    httpd = serve(_conn_factory, settings, host=args.host, port=args.port, broadcaster=broadcaster)
    watch_thread = threading.Thread(target=watcher.run, daemon=True)
    watch_thread.start()
    _emit(
        {"serving": f"http://{args.host}:{args.port}", "note": "read-only; Ctrl-C to stop"},
        as_json=as_json,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_flag.set()
        httpd.shutdown()
    return EXIT_OK


def _cmd_report(*, as_json: bool) -> int:
    from decimal import Decimal

    settings = Settings.load()
    path = db_path(settings)
    if not path.exists():
        _emit(
            {"error": "database missing — run `dexpaprika db migrate` and `snapshot` first"},
            as_json=as_json,
        )
        return EXIT_FAILURE
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT p.id, p.venue, p.chain, p.kind, p.external_id, p.group_tag,"
            " e.ts AS as_of, e.state_json"
            " FROM positions p"
            " JOIN position_events e ON e.position_id = p.id AND e.type='observed'"
            " WHERE p.closed_at IS NULL"
            " AND e.id = (SELECT MAX(id) FROM position_events"
            "             WHERE position_id = p.id AND type='observed')"
        ).fetchall()
        groups: dict[str, list[dict[str, Any]]] = {"lp_hedge": [], "defi": [], "holdings": []}
        lp_value = Decimal(0)
        defi_net = Decimal(0)
        for row in rows:
            state = json.loads(row["state_json"])
            entry: dict[str, Any] = {
                "venue": row["venue"],
                "chain": row["chain"],
                "kind": row["kind"],
                "external_id": row["external_id"],
                "as_of": row["as_of"],
                "source": state.get("source", "on-chain"),
            }
            if row["kind"] == "lp" and state.get("amount0") and state.get("price_usd"):
                value = Decimal(state["amount0"]) * Decimal(state["price_usd"]) + Decimal(
                    state.get("amount1") or "0"
                )
                entry["value_usd"] = str(value.quantize(Decimal("0.01")))
                entry["in_range"] = state.get("in_range")
                lp_value += value
            elif row["kind"] in ("lend", "borrow") and state.get("amount_usd"):
                amount = Decimal(state["amount_usd"])
                entry["amount_usd"] = str(amount)
                entry["health_factor"] = state.get("health_factor")
                defi_net += amount if row["kind"] == "lend" else -amount
            elif row["kind"] == "perp":
                entry["size_usd"] = state.get("size_usd")
                entry["mark_price"] = state.get("mark_price")
                entry["stop_loss_triggers"] = state.get("stop_loss_triggers")
            elif row["kind"] == "holding":
                entry["symbol"] = state.get("symbol")
                entry["amount"] = state.get("amount")
            groups[row["group_tag"]].append(entry)
        totals = {
            "lp_value": str(lp_value.quantize(Decimal("0.01"))),
            "defi_net": str(defi_net.quantize(Decimal("0.01"))),
        }
    finally:
        conn.close()
    _emit({"groups": groups, "totals_usd": totals}, as_json=as_json)
    return EXIT_OK


def _cmd_lp(args: argparse.Namespace, *, as_json: bool) -> int:
    from dexpaprika.chains import ChainRpcError, EvmRpcClient
    from dexpaprika.lp.discovery import discover, record
    from dexpaprika.quota import QuotaError, QuotaTracker

    settings = Settings.load()
    path = db_path(settings)
    if not path.exists():
        _emit(
            {"error": "database missing — run `dexpaprika db migrate` first (quota tracking)"},
            as_json=as_json,
        )
        return EXIT_FAILURE
    if args.address:
        wallets = [args.address]
    else:
        wallets = [
            w.address
            for w in _registry(settings).list_wallets()
            if w.chain_family == "evm" and w.included
        ]
        if not wallets:
            _emit(
                {"error": "no included EVM wallets in the registry — pass --address"},
                as_json=as_json,
            )
            return EXIT_FAILURE
    conn = connect(path)
    try:
        QuotaTracker(conn).ensure_providers()
        rpc = EvmRpcClient(
            conn,
            "base",
            settings=settings,
            clients=[_http_client_factory(url) for url in settings.base_rpc_urls],
        )
        pin = rpc.resolve_pin()
        all_positions = []
        recorded = 0
        ts = None
        for wallet in wallets:
            for position in discover(rpc, wallet, settings=settings, block=pin):
                all_positions.append(position.model_dump(mode="json"))
                if args.record:
                    from datetime import UTC, datetime

                    ts = ts or datetime.now(UTC).isoformat()
                    record(conn, wallet, position, ts)
                    recorded += 1
        payload: dict[str, Any] = {
            "block_number": pin,
            "wallets": wallets,
            "positions": all_positions,
        }
        if args.record:
            from datetime import UTC, datetime

            conn.execute(
                "INSERT INTO snapshots (ts, chain, block_number, kind) VALUES (?, 'base', ?, 'lp')",
                (ts or datetime.now(UTC).isoformat(), pin),
            )
            payload["recorded"] = recorded
    except (ChainRpcError, QuotaError) as exc:
        _emit({"error": str(exc)}, as_json=as_json)
        return EXIT_FAILURE
    finally:
        conn.close()
    _emit(payload, as_json=as_json)
    return EXIT_OK


def _cmd_chain(args: argparse.Namespace, *, as_json: bool) -> int:
    from dexpaprika.chains import ChainRpcError, EvmRpcClient
    from dexpaprika.quota import QuotaError, QuotaTracker

    settings = Settings.load()
    path = db_path(settings)
    if not path.exists():
        _emit(
            {"error": "database missing — run `dexpaprika db migrate` first (quota tracking)"},
            as_json=as_json,
        )
        return EXIT_FAILURE
    chains = ["base", "arbitrum"] if args.chain == "all" else [args.chain]
    conn = connect(path)
    try:
        QuotaTracker(conn).ensure_providers()
        results: dict[str, Any] = {}
        for chain in chains:
            urls = settings.base_rpc_urls if chain == "base" else settings.arbitrum_rpc_urls
            client = EvmRpcClient(
                conn,
                chain,
                settings=settings,
                clients=[_http_client_factory(url) for url in urls],
            )
            snap = client.snapshot("chain-snapshot")
            results[chain] = {
                "block_number": snap.block_number,
                "ts": snap.ts,
                "tripwire": "ok",
            }
    except (ChainRpcError, QuotaError) as exc:
        _emit({"error": str(exc)}, as_json=as_json)
        return EXIT_FAILURE
    finally:
        conn.close()
    _emit({"chains": results}, as_json=as_json)
    return EXIT_OK


def _cmd_gmx(args: argparse.Namespace, *, as_json: bool) -> int:
    from dexpaprika.clients.base import TransportError
    from dexpaprika.clients.gmx import GmxClient
    from dexpaprika.quota import QuotaError, QuotaTracker

    settings = Settings.load()
    path = db_path(settings)
    if not path.exists():
        _emit(
            {"error": "database missing — run `dexpaprika db migrate` first (quota tracking)"},
            as_json=as_json,
        )
        return EXIT_FAILURE
    address = args.address
    if address is None:
        included_evm = [
            w for w in _registry(settings).list_wallets() if w.chain_family == "evm" and w.included
        ]
        if len(included_evm) != 1:
            _emit(
                {
                    "error": (
                        f"{len(included_evm)} included EVM wallets in the registry —"
                        " pass --address explicitly"
                    )
                },
                as_json=as_json,
            )
            return EXIT_FAILURE
        address = included_evm[0].address
    conn = connect(path)
    try:
        QuotaTracker(conn).ensure_providers()
        client = GmxClient(
            conn,
            settings=settings,
            clients=[_http_client_factory(peer) for peer in settings.gmx_rest_peers],
        )
        positions = client.get_positions(address)
        payload: dict[str, Any] = {
            "address": address,
            "positions": [p.model_dump(mode="json", exclude={"raw"}) for p in positions],
        }
        if not positions:
            payload["note"] = (
                "empty list is a VALID state: no open positions (closed/liquidated looks like this)"
            )
        if args.record:
            for position in positions:
                client.record_observation(position)
            payload["recorded"] = len(positions)
    except (TransportError, QuotaError) as exc:
        _emit({"error": str(exc)}, as_json=as_json)
        return EXIT_FAILURE
    finally:
        conn.close()
    _emit(payload, as_json=as_json)
    return EXIT_OK


def _cmd_quota(args: argparse.Namespace, *, as_json: bool) -> int:
    from dexpaprika.quota import QuotaError, QuotaTracker

    settings = Settings.load()
    path = db_path(settings)
    if not path.exists():
        _emit(
            {"error": "database missing — run `dexpaprika db migrate` first"},
            as_json=as_json,
        )
        return EXIT_FAILURE
    conn = connect(path)
    try:
        tracker = QuotaTracker(conn)
        tracker.ensure_providers()
        providers = [tracker.summary(args.provider)] if args.provider else tracker.summaries()
    except QuotaError as exc:
        _emit({"error": str(exc)}, as_json=as_json)
        return EXIT_FAILURE
    finally:
        conn.close()
    _emit({"providers": providers}, as_json=as_json)
    return EXIT_OK


def _cmd_scheduler(args: argparse.Namespace, *, as_json: bool) -> int:
    from dexpaprika.scheduler import MISFIRE_GRACE_SECONDS, build_scheduler, job_specs

    settings = Settings.load()
    if args.scheduler_command == "jobs":
        payload = {
            "jobs": [
                {
                    "id": spec.id,
                    "argv": spec.argv,
                    "trigger": spec.trigger,
                    "minutes": spec.minutes,
                    "hour": spec.hour,
                    "minute": spec.minute,
                    "max_instances": 1,
                    "coalesce": True,
                    "misfire_grace_time": MISFIRE_GRACE_SECONDS,
                }
                for spec in job_specs(settings)
            ]
        }
        _emit(payload, as_json=as_json)
        return EXIT_OK
    # run — persistent process (container/VPS); Ctrl-C / SIGTERM exits clean.
    scheduler = build_scheduler(settings)
    _emit(
        {"scheduler": "starting", "jobs": [spec.id for spec in job_specs(settings)]},
        as_json=as_json,
    )
    import contextlib

    with contextlib.suppress(KeyboardInterrupt, SystemExit):
        scheduler.start()
    return EXIT_OK


def _sidecar_runner(settings: Settings) -> Any:
    """Production sidecar: the pinned Node executor as a one-shot subprocess.

    The execution wallet key env var is set ONLY for submit-mode payloads.
    """
    import shutil
    import subprocess  # nosec B404 — fixed argv, repo-pinned script, no shell

    def run_payload(payload: dict[str, Any]) -> dict[str, Any]:
        node = shutil.which("node")
        if node is None:
            return {"ok": False, "error": "node not found — the executor sidecar needs Node.js"}
        # On-chain (GmxSdk / Classic) executor: GMX exposes express/subaccount
        # orders to its frontend only, so order changes go on-chain — the wallet
        # signs and pays gas. Overridable via DEXPAPRIKA_SIDECAR_SCRIPT for tests.
        script_name = os.environ.get("DEXPAPRIKA_SIDECAR_SCRIPT", "gmx_exec_onchain.cjs")
        script = Path(__file__).resolve().parents[2] / "executor" / script_name
        if not script.exists():
            return {"ok": False, "error": f"sidecar script missing at {script}"}
        # Inherit the parent environment — Node's CSPRNG aborts at startup on
        # Windows without SystemRoot (and proxy/CA vars must survive too). Strip
        # every dexpaprika secret so the sidecar only ever sees a key we hand it
        # explicitly for submit mode.
        env = {k: v for k, v in os.environ.items() if not k.startswith("DEXPAPRIKA_SECRET_")}
        # Execution target (S9.5) — mainnet by default, Sepolia for testnet.
        env["GMX_CHAIN_ID"] = str(settings.gmx_chain_id)
        env["GMX_ACCOUNT"] = settings.execution_account
        # Optional API/RPC overrides pass through if the operator set them.
        for var in ("GMX_RPC_URL", "GMX_ORACLE_URL", "GMX_SUBSQUID_URL"):
            if var in os.environ:
                env[var] = os.environ[var]
        env.pop("GMX_WALLET_KEY", None)  # never inherited; only added below
        env.pop("GMX_SUBACCOUNT_KEY", None)
        if payload.get("mode") == "submit":
            key = resolve_provider(settings).get("gmx_wallet_key")
            if key is None:
                return {
                    "ok": False,
                    "error": (
                        "secret 'gmx_wallet_key' not resolvable — set"
                        " DEXPAPRIKA_SECRET_GMX_WALLET_KEY to the execution wallet key"
                    ),
                }
            env["GMX_WALLET_KEY"] = key
        try:
            proc = subprocess.run(  # noqa: S603 # nosec B603 — fixed argv, no shell
                [node, str(script)],
                input=json.dumps(payload, default=str),
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": f"sidecar failed to run: {exc}"}
        if proc.returncode != 0:
            return {"ok": False, "error": f"sidecar exit {proc.returncode}: {proc.stderr[-500:]}"}
        try:
            result: dict[str, Any] = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"sidecar returned invalid JSON: {exc}"}
        return result

    return run_payload


def _cmd_execute(args: argparse.Namespace, *, as_json: bool) -> int:
    from datetime import UTC, datetime
    from decimal import Decimal

    from dexpaprika.execute.approval import ApprovalDecision, request_approval
    from dexpaprika.execute.engine import execute_instruction
    from dexpaprika.execute.instruction import OrderInstruction
    from dexpaprika.execute.safety import arm as do_arm
    from dexpaprika.execute.safety import check_armed, check_kill_switch

    settings = Settings.load()
    now = datetime.now(UTC)

    if args.execute_command == "arm":
        try:
            armed_path = do_arm(settings, ttl_minutes=args.ttl_minutes, now=now)
        except RuntimeError as exc:
            _emit({"error": str(exc)}, as_json=as_json)
            return EXIT_FAILURE
        # Arming is a privileged state change — audited (verifier finding #2).
        arm_db = db_path(settings)
        if arm_db.exists():
            arm_conn = connect(arm_db)
            try:
                arm_conn.execute(
                    "INSERT INTO audit_log (ts, actor, action, phase, payload_json)"
                    " VALUES (?, 'executor', 'arm', 'intent', ?)",
                    (now.isoformat(), json.dumps({"ttl_minutes": args.ttl_minutes})),
                )
            finally:
                arm_conn.close()
        _emit(
            {"armed": True, "path": str(armed_path), "ttl_minutes": args.ttl_minutes},
            as_json=as_json,
        )
        return EXIT_OK

    if args.execute_command == "status":
        kill = check_kill_switch(settings)
        armed = check_armed(settings, arm_flag=True, now=now)
        _emit(
            {
                "armed": armed.allowed,
                "armed_detail": armed.reason,
                "kill_switch": not kill.allowed,
                "kill_detail": kill.reason,
                "limits": {
                    "max_position_usd": str(settings.max_position_usd),
                    "max_delta_per_run_usd": str(settings.max_delta_per_run_usd),
                    "max_daily_adjustments": settings.max_daily_adjustments,
                    "allowed_markets": list(settings.allowed_markets),
                    "order_rate_limit_seconds": settings.order_rate_limit_seconds,
                },
            },
            as_json=as_json,
        )
        return EXIT_OK

    # Mutating commands: database required (audit trail is not optional).
    path = db_path(settings)
    if not path.exists():
        _emit(
            {"error": "database missing — run `dexpaprika db migrate` first (audit trail)"},
            as_json=as_json,
        )
        return EXIT_FAILURE
    sidecar = _sidecar_runner(settings)

    conn = connect(path)
    try:
        if args.execute_command == "set-sl-trigger":
            order_key = args.key
            if order_key is None:
                # Kill switch halts even this read-only resolution call
                # (verifier finding #5: no network activity while tripped).
                kill = check_kill_switch(settings)
                if not kill.allowed:
                    _emit({"error": kill.reason}, as_json=as_json)
                    return EXIT_DEGRADED
                read = sidecar({"mode": "read", "action": "read-orders", "params": {}})
                orders = read.get("orders", []) if read.get("ok") else []
                if not orders:
                    _emit(
                        {"error": "no open order found to retarget — pass --key explicitly"},
                        as_json=as_json,
                    )
                    return EXIT_FAILURE
                order_key = str(orders[0]["key"])
            instruction = OrderInstruction(
                action="set-sl-trigger",
                order_key=order_key,
                trigger_price=Decimal(args.price),
            )
            delta_usd = Decimal(0)
            new_position = _current_short_usd(conn)
        elif args.execute_command == "resize-short":
            from dexpaprika.hedge.state import latest_inputs

            inputs = latest_inputs(conn)
            if inputs is None:
                _emit(
                    {"error": "no recorded hedge state — run `dexpaprika snapshot` first"},
                    as_json=as_json,
                )
                return EXIT_FAILURE
            _lp, short, price = inputs
            current_eth = short.size_eth if short is not None else Decimal(0)
            target = Decimal(args.target_eth)
            instruction = OrderInstruction(action="resize-short", target_eth=target)
            delta_usd = abs(target - current_eth) * price
            new_position = target * price
        else:  # cancel-order
            instruction = OrderInstruction(action="cancel-order", order_key=args.key)
            delta_usd = Decimal(0)
            new_position = _current_short_usd(conn)

        def approval(instruction_id: str, message: str) -> ApprovalDecision:
            topic = resolve_provider(settings).get("ntfy_topic")
            if topic is None:
                return ApprovalDecision(
                    approved=False,
                    reason="no ntfy topic configured — cannot request approval; fail-closed",
                )
            from dexpaprika.alerts.ntfy import NtfyClient

            client = NtfyClient(
                conn,
                settings=settings,
                client=_http_client_factory(settings.ntfy_server),
                topic=topic,
            )
            import time as _time

            return request_approval(
                instruction_id,
                message,
                publisher=lambda title, body, priority: client.publish(
                    title, body, priority=priority, tags=["rotating_light"]
                ),
                poller=client.poll,
                clock=lambda: datetime.now(UTC),
                sleeper=_time.sleep,
                timeout_minutes=settings.approval_timeout_minutes,
            )

        result = execute_instruction(
            conn,
            instruction,
            settings=settings,
            sidecar=sidecar,
            approval=approval,
            arm_flag=args.arm,
            now=now,
            delta_usd=delta_usd,
            new_position_usd=new_position,
        )
    finally:
        conn.close()
    _emit(result.model_dump(mode="json"), as_json=as_json)
    if result.status in ("dry-run", "confirmed", "replayed"):
        return EXIT_OK
    if result.status == "failed":
        return EXIT_FAILURE
    return EXIT_DEGRADED  # blocked / rejected


def _current_short_usd(conn: Any) -> Any:
    from decimal import Decimal

    row = conn.execute(
        "SELECT e.state_json FROM positions p"
        " JOIN position_events e ON e.position_id = p.id AND e.type='observed'"
        " WHERE p.kind='perp' AND p.closed_at IS NULL ORDER BY e.id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return Decimal(0)
    state = json.loads(row["state_json"])
    return Decimal(str(state["size_usd"])) if state.get("size_usd") else Decimal(0)


def _offline_health(settings: Settings) -> dict[str, str]:
    """The healthcheck subset that needs no network — alert-rule input."""
    return {
        "db_integrity": _check_db_integrity(settings),
        "migrations_current": _check_migrations_current(settings),
        "secrets_present": _check_secrets_present(settings),
        "data_dir_writable": _check_data_dir_writable(settings),
    }


def _cmd_alerts(args: argparse.Namespace, *, as_json: bool) -> int:
    from datetime import UTC, datetime

    from dexpaprika.alerts.ntfy import NtfyClient
    from dexpaprika.alerts.rules import (
        apply_cooldown,
        evaluate,
        mark_delivery,
        record_alert,
    )
    from dexpaprika.clients.base import TransportError
    from dexpaprika.quota import QuotaError, QuotaTracker

    settings = Settings.load()
    path = db_path(settings)
    if not path.exists():
        _emit(
            {"error": "database missing — run `dexpaprika db migrate` first"},
            as_json=as_json,
        )
        return EXIT_FAILURE
    conn = connect(path)
    try:
        if pending(conn):
            _emit(
                {"error": "schema out of date — run `dexpaprika db migrate` first"},
                as_json=as_json,
            )
            return EXIT_FAILURE
        QuotaTracker(conn).ensure_providers()

        if args.alerts_command == "log":
            rows = conn.execute(
                "SELECT id, ts, rule, severity, payload_json, delivered, ntfy_status"
                " FROM alerts_log ORDER BY id DESC LIMIT ?",
                (args.limit,),
            ).fetchall()
            _emit({"alerts": [dict(row) for row in rows]}, as_json=as_json)
            return EXIT_OK

        topic = resolve_provider(settings).get("ntfy_topic")

        def make_client(topic_value: str) -> NtfyClient:
            return NtfyClient(
                conn,
                settings=settings,
                client=_http_client_factory(settings.ntfy_server),
                topic=topic_value,
            )

        if args.alerts_command == "test":
            if topic is None:
                _emit(
                    {
                        "error": (
                            "secret 'ntfy_topic' not resolvable — store it in the OS keyring"
                            " (service 'dexpaprika') or set DEXPAPRIKA_SECRET_NTFY_TOPIC"
                        )
                    },
                    as_json=as_json,
                )
                return EXIT_FAILURE
            try:
                receipt = make_client(topic).publish(
                    "dexpaprika test",
                    "Alert channel verification — no action needed.",
                    priority="min",
                    tags=["white_check_mark"],
                )
            except TransportError as exc:
                _emit({"error": str(exc)}, as_json=as_json)
                return EXIT_FAILURE
            _emit({"sent": True, "receipt": receipt.model_dump(mode="json")}, as_json=as_json)
            return EXIT_OK

        # check
        now = datetime.now(UTC)
        alerts = evaluate(conn, settings=settings, now=now, health=_offline_health(settings))
        if args.dry_run:
            _emit(
                {"dry_run": True, "alerts": [a.model_dump() for a in alerts]},
                as_json=as_json,
            )
            return EXIT_OK
        fire, suppressed = apply_cooldown(conn, alerts, settings=settings, now=now)
        client = make_client(topic) if fire and topic is not None else None
        degraded = False
        fired_payload: list[dict[str, Any]] = []
        for alert in fire:
            # Record BEFORE delivery: a dead channel loses a notification,
            # never the record of why it fired.
            alert_id = record_alert(conn, alert, now=now)
            if client is None:
                mark_delivery(conn, alert_id, delivered=False, ntfy_status="no-topic")
                degraded = True
                delivery = "no-topic"
            else:
                try:
                    client.publish(
                        alert.title,
                        alert.message,
                        priority=alert.severity,
                        tags=alert.tags,
                    )
                    mark_delivery(conn, alert_id, delivered=True, ntfy_status="200")
                    delivery = "delivered"
                except TransportError as exc:
                    mark_delivery(conn, alert_id, delivered=False, ntfy_status=str(exc))
                    degraded = True
                    delivery = "failed"
            fired_payload.append({**alert.model_dump(), "delivery": delivery})
        _emit(
            {
                "fired": fired_payload,
                "suppressed": [a.model_dump() for a in suppressed],
                "degraded": degraded,
            },
            as_json=as_json,
        )
        return EXIT_DEGRADED if degraded else EXIT_OK
    except QuotaError as exc:
        _emit({"error": str(exc)}, as_json=as_json)
        return EXIT_FAILURE
    finally:
        conn.close()


# ------------------------------- parser -------------------------------


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Machine-readable JSON output.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dexpaprika",
        description="Claude-operated DeFi portfolio, recording, and hedge system.",
    )
    parser.add_argument("--version", action="version", version=f"dexpaprika {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("status", "One-call operational overview."),
        ("healthcheck", "Full system health verification (exit 0 only if all pass)."),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        _add_json_flag(sub)

    wallets = subparsers.add_parser("wallets", help="Manage the tracked-wallet registry.")
    wallets_sub = wallets.add_subparsers(dest="wallets_command", required=True)

    w_list = wallets_sub.add_parser("list", help="List registered wallets.")
    _add_json_flag(w_list)

    w_add = wallets_sub.add_parser("add", help="Register a wallet (validated + normalized).")
    w_add.add_argument("--chain", required=True, choices=("evm", "btc", "solana"))
    w_add.add_argument("--address", required=True)
    w_add.add_argument("--label", default=None)
    _add_json_flag(w_add)

    for name, help_text in (
        ("remove", "Remove a wallet from the registry."),
        ("include", "Include a wallet in tracking."),
        ("exclude", "Exclude a wallet from tracking (kept in the registry)."),
    ):
        sub = wallets_sub.add_parser(name, help=help_text)
        sub.add_argument("--address", default=None)
        sub.add_argument("--label", default=None)
        _add_json_flag(sub)

    db = subparsers.add_parser("db", help="Database lifecycle: status, migrate, backup, restore.")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    for name, help_text in (
        ("status", "Schema version, pending migrations, integrity."),
        ("migrate", "Apply pending migrations (idempotent)."),
        ("backup", "Create a verified online backup."),
    ):
        sub = db_sub.add_parser(name, help=help_text)
        _add_json_flag(sub)
    db_restore = db_sub.add_parser("restore", help="Verified restore (newest backup by default).")
    db_restore.add_argument("--from", dest="source", default=None, metavar="PATH")
    _add_json_flag(db_restore)

    quota = subparsers.add_parser(
        "quota", help="Provider quota: spend vs budget from the call log."
    )
    quota.add_argument("--provider", default=None, help="Limit to one provider.")
    _add_json_flag(quota)

    market = subparsers.add_parser(
        "market", help="DexPaprika market data (history/volume — NOT hedge-math prices)."
    )
    market_sub = market.add_subparsers(dest="market_command", required=True)
    m_pool = market_sub.add_parser("pool", help="Pool details (volume, txns; fee often null).")
    m_pool.add_argument("--network", required=True)
    m_pool.add_argument("--address", required=True)
    m_pool.add_argument("--record", action="store_true", help="Persist to pool_metrics.")
    _add_json_flag(m_pool)
    m_ohlcv = market_sub.add_parser("ohlcv", help="Candles for a pool.")
    m_ohlcv.add_argument("--network", required=True)
    m_ohlcv.add_argument("--address", required=True)
    m_ohlcv.add_argument("--start", required=True, help="YYYY-MM-DD")
    m_ohlcv.add_argument("--interval", default="24h")
    m_ohlcv.add_argument("--limit", type=int, default=30)
    m_ohlcv.add_argument("--record", action="store_true", help="Upsert into ohlcv table.")
    _add_json_flag(m_ohlcv)

    gmx = subparsers.add_parser("gmx", help="GMX v2 hedge-leg data (positions + orders).")
    gmx_sub = gmx.add_subparsers(dest="gmx_command", required=True)
    g_pos = gmx_sub.add_parser(
        "positions", help="Open positions incl. related orders (scaled Decimals)."
    )
    g_pos.add_argument(
        "--address", default=None, help="Defaults to the single included EVM wallet."
    )
    g_pos.add_argument("--record", action="store_true", help="Persist observation to DB.")
    _add_json_flag(g_pos)

    chain = subparsers.add_parser(
        "chain", help="Block-pinned on-chain snapshots (tripwire-verified)."
    )
    chain_sub = chain.add_subparsers(dest="chain_command", required=True)
    c_snap = chain_sub.add_parser("snapshot", help="Pin + verify + record per chain.")
    c_snap.add_argument("--chain", default="all", choices=("base", "arbitrum", "all"))
    _add_json_flag(c_snap)

    lp = subparsers.add_parser("lp", help="CL LP positions (custody-aware discovery).")
    lp_sub = lp.add_subparsers(dest="lp_command", required=True)
    lp_snap = lp_sub.add_parser("snapshot", help="Discover + value LP positions at one pin.")
    lp_snap.add_argument("--address", default=None, help="Default: all included EVM wallets.")
    lp_snap.add_argument("--record", action="store_true", help="Persist positions + snapshot row.")
    _add_json_flag(lp_snap)

    snapshot = subparsers.add_parser(
        "snapshot", help="Record the portfolio (LP, hedge, defi, holdings) + lifecycle events."
    )
    snapshot.add_argument(
        "--kind", default="all", choices=("lp", "hedge", "defi", "holdings", "all")
    )
    snapshot.add_argument("--address", default=None, help="Default: all included EVM wallets.")
    _add_json_flag(snapshot)

    recorder = subparsers.add_parser(
        "recorder", help="Full-variable recorder: single cycle, service loop, or status."
    )
    recorder_sub = recorder.add_subparsers(dest="recorder_command", required=True)
    _kind_choices = ("lp", "hedge", "defi", "holdings", "all")
    r_cycle = recorder_sub.add_parser("cycle", help="One recording cycle (scheduler fallback).")
    r_cycle.add_argument("--kind", default="all", choices=_kind_choices)
    r_cycle.add_argument("--address", default=None, help="Default: all included EVM wallets.")
    _add_json_flag(r_cycle)
    r_run = recorder_sub.add_parser("run", help="Run the recorder service loop (foreground).")
    r_run.add_argument("--kind", default="all", choices=_kind_choices)
    r_run.add_argument("--address", default=None, help="Default: all included EVM wallets.")
    r_run.add_argument("--lp-interval", type=float, default=60.0, help="Seconds between LP cycles.")
    r_run.add_argument(
        "--hedge-interval", type=float, default=30.0, help="Seconds between hedge cycles."
    )
    r_run.add_argument("--max-cycles", type=int, default=None, help="Bound the loop (tests/smoke).")
    _add_json_flag(r_run)
    r_status = recorder_sub.add_parser("status", help="Last cycle per source + staleness.")
    _add_json_flag(r_status)

    dashboard = subparsers.add_parser(
        "dashboard", help="Live read-only dashboard (SSE) or a static HTML export."
    )
    dashboard_sub = dashboard.add_subparsers(dest="dashboard_command", required=True)
    d_serve = dashboard_sub.add_parser("serve", help="Run the read-only dashboard server (blocks).")
    d_serve.add_argument("--host", default="127.0.0.1")
    d_serve.add_argument("--port", type=int, default=8787)
    _add_json_flag(d_serve)
    d_export = dashboard_sub.add_parser(
        "export", help="Write a self-contained static HTML snapshot."
    )
    d_export.add_argument("--out", default=None, help="Default: <data_dir>/dashboard.html")
    _add_json_flag(d_export)

    report = subparsers.add_parser("report", help="Latest portfolio grouped with as_of/source.")
    _add_json_flag(report)

    hedge = subparsers.add_parser(
        "hedge", help="Hedge coverage analysis (read-only; school-material rules)."
    )
    hedge_sub = hedge.add_subparsers(dest="hedge_command", required=True)
    h_status = hedge_sub.add_parser("status", help="Coverage/quadrant/flags from latest states.")
    _add_json_flag(h_status)
    h_sim = hedge_sub.add_parser("simulate", help="What-if P&L (dual-curve) at prices.")
    h_sim.add_argument("--price", default=None, help="Single price to evaluate.")
    h_sim.add_argument("--curve", type=int, default=9, help="N points floor→ceiling.")
    _add_json_flag(h_sim)

    alerts = subparsers.add_parser(
        "alerts", help="Alert rules over recorded state + ntfy delivery."
    )
    alerts_sub = alerts.add_subparsers(dest="alerts_command", required=True)
    a_check = alerts_sub.add_parser(
        "check", help="Evaluate rules → record → deliver (scheduler entrypoint)."
    )
    a_check.add_argument(
        "--dry-run", action="store_true", help="Evaluate and print; record/send nothing."
    )
    _add_json_flag(a_check)
    a_test = alerts_sub.add_parser("test", help="Send one live test notification.")
    _add_json_flag(a_test)
    a_log = alerts_sub.add_parser("log", help="Alert firing history (audit).")
    a_log.add_argument("--limit", type=int, default=20)
    _add_json_flag(a_log)

    scheduler = subparsers.add_parser(
        "scheduler", help="Container/VPS scheduler (playbook Option B; Windows uses schtasks)."
    )
    scheduler_sub = scheduler.add_subparsers(dest="scheduler_command", required=True)
    s_jobs = scheduler_sub.add_parser("jobs", help="Show the configured jobs (offline).")
    _add_json_flag(s_jobs)
    s_run = scheduler_sub.add_parser("run", help="Run the persistent scheduler (blocks).")
    _add_json_flag(s_run)

    execute = subparsers.add_parser(
        "execute",
        help="PRIVILEGED hedge orders (dry-run default; --arm + armed state to go live).",
    )
    execute_sub = execute.add_subparsers(dest="execute_command", required=True)
    e_arm = execute_sub.add_parser("arm", help="Create the armed-state file (separate step).")
    e_arm.add_argument("--ttl-minutes", type=int, default=None)
    _add_json_flag(e_arm)
    e_status = execute_sub.add_parser("status", help="Armed / kill-switch / limits overview.")
    _add_json_flag(e_status)
    e_sl = execute_sub.add_parser("set-sl-trigger", help="Retarget the stop-loss trigger.")
    e_sl.add_argument("--price", required=True, help="New trigger price in USD.")
    e_sl.add_argument("--key", default=None, help="Order key (default: the open SL order).")
    e_sl.add_argument("--arm", action="store_true", help="Live mode (needs armed state).")
    _add_json_flag(e_sl)
    e_resize = execute_sub.add_parser("resize-short", help="Resize the hedge to a target ETH.")
    e_resize.add_argument("--target-eth", required=True)
    e_resize.add_argument("--arm", action="store_true")
    _add_json_flag(e_resize)
    e_cancel = execute_sub.add_parser("cancel-order", help="Cancel an open order by key.")
    e_cancel.add_argument("--key", required=True)
    e_cancel.add_argument("--arm", action="store_true")
    _add_json_flag(e_cancel)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "status":
        return _cmd_status(as_json=args.as_json)
    if args.command == "healthcheck":
        return _cmd_healthcheck(as_json=args.as_json)
    if args.command == "db":
        return _cmd_db(args, as_json=args.as_json)
    if args.command == "quota":
        return _cmd_quota(args, as_json=args.as_json)
    if args.command == "market":
        return _cmd_market(args, as_json=args.as_json)
    if args.command == "gmx":
        return _cmd_gmx(args, as_json=args.as_json)
    if args.command == "chain":
        return _cmd_chain(args, as_json=args.as_json)
    if args.command == "lp":
        return _cmd_lp(args, as_json=args.as_json)
    if args.command == "snapshot":
        return _cmd_snapshot(args, as_json=args.as_json)
    if args.command == "recorder":
        return _cmd_recorder(args, as_json=args.as_json)
    if args.command == "dashboard":
        return _cmd_dashboard(args, as_json=args.as_json)
    if args.command == "report":
        return _cmd_report(as_json=args.as_json)
    if args.command == "hedge":
        return _cmd_hedge(args, as_json=args.as_json)
    if args.command == "alerts":
        return _cmd_alerts(args, as_json=args.as_json)
    if args.command == "scheduler":
        return _cmd_scheduler(args, as_json=args.as_json)
    if args.command == "execute":
        return _cmd_execute(args, as_json=args.as_json)
    # Required subparsers guarantee the only remaining command is "wallets".
    return _cmd_wallets(args, as_json=args.as_json)


if __name__ == "__main__":
    raise SystemExit(main())
