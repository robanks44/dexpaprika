"""Forward-only, versioned migrations (S2 spec).

Migrations are packaged SQL files ``storage/sql/NNNN_name.sql`` applied in
version order, each inside one explicit transaction: a failure rolls the
whole file back (no partial DDL, no ``schema_migrations`` row) and the
database stays usable. Re-running ``migrate`` is a no-op.

File constraint (documented in 0001): one statement per ``;``, no triggers —
the splitter is deliberately simple and our SQL is controlled input.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from importlib import resources

_SQL_PACKAGE = "dexpaprika.storage"
_SQL_DIR = "sql"


class MigrationError(Exception):
    """A migration failed to apply (fully rolled back)."""


def packaged_migrations() -> dict[int, tuple[str, str]]:
    """All packaged migrations: version -> (name, sql)."""
    migrations: dict[int, tuple[str, str]] = {}
    sql_dir = resources.files(_SQL_PACKAGE).joinpath(_SQL_DIR)
    for entry in sql_dir.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        stem = entry.name[: -len(".sql")]
        version_text, _, name = stem.partition("_")
        migrations[int(version_text)] = (name, entry.read_text(encoding="utf-8"))
    return migrations


def _ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " applied_at TEXT NOT NULL)"
    )


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    _ensure_schema_migrations(conn)
    return {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}


def current_version(conn: sqlite3.Connection) -> int:
    """Highest applied migration version (0 for a fresh database)."""
    applied = _applied_versions(conn)
    return max(applied) if applied else 0


def pending(conn: sqlite3.Connection) -> list[str]:
    """Names of packaged migrations not yet applied, in order."""
    applied = _applied_versions(conn)
    return [
        name
        for version, (name, _sql) in sorted(packaged_migrations().items())
        if version not in applied
    ]


def _split_statements(sql: str) -> list[str]:
    # Strip `--` line comments FIRST (comments may contain ';'), then split.
    # File constraint (documented in 0001): no `--` inside string literals,
    # one statement per ';', no triggers.
    uncommented = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    return [chunk.strip() for chunk in uncommented.split(";") if chunk.strip()]


def migrate(
    conn: sqlite3.Connection,
    extra_migrations: dict[int, tuple[str, str]] | None = None,
) -> list[str]:
    """Apply all pending migrations; return the names applied (in order).

    ``extra_migrations`` exists for tests (injected failure cases); production
    always uses the packaged files.
    """
    catalogue = packaged_migrations()
    if extra_migrations:
        catalogue = {**catalogue, **extra_migrations}
    applied_now: list[str] = []
    applied = _applied_versions(conn)
    for version, (name, sql) in sorted(catalogue.items()):
        if version in applied:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _split_statements(sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, datetime.now(UTC).isoformat()),
            )
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            conn.execute("ROLLBACK")
            msg = f"migration {version:04d}_{name} failed and was rolled back: {exc}"
            raise MigrationError(msg) from exc
        applied_now.append(name)
    return applied_now
