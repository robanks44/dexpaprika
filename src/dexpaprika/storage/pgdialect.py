"""SQLite → Postgres/TimescaleDB dialect translation (S11, §6 migration path).

Pure string transforms over the SAME packaged migrations SQLite applies —
one schema source of truth, two dialects. Reference:
``timescaledb--api-reference--lp-tracker.md`` (hypertables convert EMPTY
tables via ``create_hypertable(by_range(<time col>))``; every unique index
on a hypertable must include the partition column).

Hypertable set: only append-only tables whose surrogate ``id`` is never a
foreign-key target — ``snapshots``/``positions``/… stay regular because
other tables REFERENCE their ids.
"""

from __future__ import annotations

import re

from dexpaprika.storage.migrations import _split_statements, packaged_migrations

# table -> (time column, chunk interval). Chunks per the reference doc:
# hour-scale for the busiest stream, day-scale otherwise.
HYPERTABLES: dict[str, tuple[str, str]] = {
    "api_call_log": ("ts", "1 day"),
    "pool_metrics": ("ts", "1 hour"),
    "ohlcv": ("ts_start", "1 day"),
}

# Time-partition columns (ISO-8601 TEXT in SQLite) become TIMESTAMPTZ so
# by_range() can partition; ISO strings insert into TIMESTAMPTZ unchanged.
_TIME_COLUMNS = re.compile(r"\b(ts|ts_start|ts_end) TEXT\b")
# `interval` is a Postgres type keyword — quote it wherever it appears as a
# bare lowercase identifier (our authored DDL uses uppercase INTERVAL for
# the type, so the two never collide).
_INTERVAL_IDENT = re.compile(r"(?<![\w\"'])interval(?![\w\"'])")


def translate_statement(statement: str) -> str:
    """One SQLite DDL statement → its Postgres equivalent."""
    out = statement.replace("INTEGER PRIMARY KEY", "BIGSERIAL PRIMARY KEY")
    out = _TIME_COLUMNS.sub(lambda m: f"{m.group(1)} TIMESTAMPTZ", out)
    return _INTERVAL_IDENT.sub('"interval"', out)


def translated_migrations() -> list[tuple[int, str, list[str]]]:
    """Every packaged migration, translated: (version, name, statements)."""
    return [
        (version, name, [translate_statement(s) for s in _split_statements(sql)])
        for version, (name, sql) in sorted(packaged_migrations().items())
    ]


def hypertable_ddl() -> list[str]:
    """Post-migration conversion DDL — run on the still-EMPTY tables.

    The surrogate ``id`` is dropped first: a hypertable's unique indexes
    must include the partition column, which a lone BIGSERIAL PK cannot.
    Existing UNIQUE constraints (ohlcv's includes ``ts_start``) survive.
    """
    ddl: list[str] = []
    for table, (column, chunk) in HYPERTABLES.items():
        ddl.append(f"ALTER TABLE {table} DROP COLUMN id")
        # Rehearsal-verified (TimescaleDB 2.17): the generalized API takes the
        # chunk interval INSIDE by_range(), not as a separate parameter.
        ddl.append(f"SELECT create_hypertable('{table}', by_range('{column}', INTERVAL '{chunk}'))")
    return ddl
