"""CLI entrypoint — S0 stubs only.

Contract (ENGINEERING_STANDARDS §0): single entrypoint, machine-readable
``--json`` output, meaningful exit codes, ``status``/``healthcheck`` commands.
Exit codes: 0 ok, 1 operational failure, 2 usage error, 3 degraded.

S0 ships stubs: ``status`` reports scaffold state; ``healthcheck`` honestly
reports every check as not-implemented and exits 3 (degraded) — it may exit 0
only when all checks pass (standards §2), which no scaffold can claim.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from dexpaprika import __version__

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
    "repo_state",
    "operational_state",  # dry-run vs armed, kill-switch, exposure vs limits
)


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    else:
        for key, value in payload.items():
            sys.stdout.write(f"{key}: {value}\n")


def _cmd_status(*, as_json: bool) -> int:
    _emit(
        {
            "app": "dexpaprika",
            "version": __version__,
            "phase": "scaffold",
            "sections_complete": ["s0"],
            "detail": "S0 scaffold only — no application capabilities are built yet.",
        },
        as_json=as_json,
    )
    return EXIT_OK


def _cmd_healthcheck(*, as_json: bool) -> int:
    checks = dict.fromkeys(_HEALTHCHECKS, "not-implemented")
    _emit(
        {
            "app": "dexpaprika",
            "version": __version__,
            "healthy": False,
            "degraded": True,
            "checks": checks,
            "detail": "Scaffold healthcheck: all checks pending implementation (S1+).",
        },
        as_json=as_json,
    )
    return EXIT_DEGRADED


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
        sub.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help="Machine-readable JSON output.",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Required subparsers guarantee args.command is a registered handler:
    # an unknown command already exited with EXIT_USAGE inside parse_args.
    handlers = {"status": _cmd_status, "healthcheck": _cmd_healthcheck}
    return handlers[args.command](as_json=args.as_json)


if __name__ == "__main__":
    raise SystemExit(main())
