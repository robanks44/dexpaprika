"""CLI entrypoint (ENGINEERING_STANDARDS §0: agent-first, ``--json``, exit codes).

Exit codes: 0 ok, 1 operational failure, 2 usage error, 3 degraded.
``simulate``/``status`` and ``execute`` will be SEPARATE commands (S7/S9).

S1 surface: ``status``, ``healthcheck``, ``wallets list|add|remove|include|exclude``.
``healthcheck`` exits 0 only when ALL checks pass; checks not yet implemented
report ``not-implemented`` and keep the overall state degraded (exit 3).
"""

from __future__ import annotations

import argparse
import json
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


def _cmd_healthcheck(*, as_json: bool) -> int:
    settings = Settings.load()
    checks = dict.fromkeys(_HEALTHCHECKS, "not-implemented")
    checks["secrets_present"] = _check_secrets_present(settings)
    checks["data_dir_writable"] = _check_data_dir_writable(settings)
    checks["db_integrity"] = _check_db_integrity(settings)
    checks["migrations_current"] = _check_migrations_current(settings)
    healthy = all(value == "ok" for value in checks.values())
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
    # Required subparsers guarantee the only remaining command is "wallets".
    return _cmd_wallets(args, as_json=args.as_json)


if __name__ == "__main__":
    raise SystemExit(main())
