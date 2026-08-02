"""Migrations runner — idempotence, atomic rollback, schema v1 contents."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from dexpaprika.storage.db import connect
from dexpaprika.storage.migrations import (
    MigrationError,
    current_version,
    migrate,
    pending,
)

SCHEMA_V1_TABLES = {
    "providers",
    "provider_endpoint_costs",
    "api_call_log",
    "snapshots",
    "positions",
    "position_events",
    "hedge_state",
    "orders",
    "alerts_log",
    "audit_log",
    "schema_migrations",
}


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "t.db")
    yield connection
    connection.close()


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row["name"] for row in rows if not row["name"].startswith("sqlite_")}


def test_fresh_db_migrates_to_full_schema(conn: sqlite3.Connection) -> None:
    applied = migrate(conn)
    assert applied  # at least 0001
    assert _table_names(conn) >= SCHEMA_V1_TABLES
    assert current_version(conn) >= 1
    assert pending(conn) == []


def test_migrate_is_idempotent(conn: sqlite3.Connection) -> None:
    migrate(conn)
    assert migrate(conn) == []  # second run: nothing to do
    assert pending(conn) == []


def test_schema_migrations_records_applied(conn: sqlite3.Connection) -> None:
    migrate(conn)
    rows = conn.execute(
        "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert rows[0]["version"] == 1
    assert "initial" in rows[0]["name"]
    assert rows[0]["applied_at"]  # ISO timestamp recorded


def test_failing_migration_rolls_back_atomically(conn: sqlite3.Connection, tmp_path: Path) -> None:
    migrate(conn)
    good = "CREATE TABLE extra_ok (id INTEGER PRIMARY KEY);"
    bad = "CREATE TABLE broken (id INTEGER PRIMARY KEY);\nSELECT * FROM does_not_exist;"
    version = current_version(conn)
    extra = {version + 1: ("extra", good), version + 2: ("broken", bad)}
    with pytest.raises(MigrationError, match="broken"):
        migrate(conn, extra_migrations=extra)
    # The good one applied; the bad one fully rolled back.
    assert "extra_ok" in _table_names(conn)
    assert "broken" not in _table_names(conn)
    assert current_version(conn) == version + 1
    # DB still usable.
    conn.execute("SELECT 1")


def test_money_columns_are_text_not_real(conn: sqlite3.Connection) -> None:
    """No money/price/qty column may be REAL/FLOAT (ENGINEERING_STANDARDS §1)."""
    migrate(conn)
    suspicious: list[str] = []
    for table in sorted(SCHEMA_V1_TABLES - {"schema_migrations"}):
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
            name, ctype = row["name"], (row["type"] or "").upper()
            money_like = any(
                token in name for token in ("usd", "price", "amount", "size", "qty", "credit")
            )
            if money_like and ("REAL" in ctype or "FLOA" in ctype or "DOUB" in ctype):
                suspicious.append(f"{table}.{name} is {ctype}")
    assert suspicious == []


def test_foreign_keys_enforced_after_migrate(conn: sqlite3.Connection) -> None:
    migrate(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO position_events (position_id, ts, type, delta_json, state_json)"
            " VALUES (99999, '2026-08-02T00:00:00+00:00', 'open', '{}', '{}')"
        )


def test_time_series_tables_have_ts_column(conn: sqlite3.Connection) -> None:
    """Timescale migration path: hypertables need a time partition column."""
    migrate(conn)
    for table in ("snapshots", "position_events", "hedge_state", "api_call_log", "alerts_log"):
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert "ts" in columns, table
