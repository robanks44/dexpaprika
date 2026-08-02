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
from typing import Any

from dexpaprika import __version__
from dexpaprika.config import Settings
from dexpaprika.secrets import resolve_provider
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


def _cmd_healthcheck(*, as_json: bool) -> int:
    settings = Settings.load()
    checks = dict.fromkeys(_HEALTHCHECKS, "not-implemented")
    checks["secrets_present"] = _check_secrets_present(settings)
    checks["data_dir_writable"] = _check_data_dir_writable(settings)
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "status":
        return _cmd_status(as_json=args.as_json)
    if args.command == "healthcheck":
        return _cmd_healthcheck(as_json=args.as_json)
    # Required subparsers guarantee the only remaining command is "wallets".
    return _cmd_wallets(args, as_json=args.as_json)


if __name__ == "__main__":
    raise SystemExit(main())
