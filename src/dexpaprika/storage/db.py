"""Connection factory and Decimal discipline (ENGINEERING_STANDARDS §1/§2).

Money never touches float: values are stored as plain decimal TEXT
(``decimal_to_text``) chosen over BLOB for auditability, and because
Postgres NUMERIC casts directly from these strings on Timescale migration.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

from dexpaprika.config import Settings

DB_FILENAME = "dexpaprika.db"


def db_path(settings: Settings) -> Path:
    """The single database file, inside the configured data dir."""
    return settings.data_dir / DB_FILENAME


def connect(path: Path) -> sqlite3.Connection:
    """Open a connection with the project PRAGMAs applied.

    WAL persists in the database file once set; the remaining PRAGMAs are
    per-connection and must be applied on every connect
    (reference: sqlite--best-practices.md §1/§10).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, autocommit=True)  # explicit BEGIN/COMMIT only
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def decimal_to_text(value: Decimal) -> str:
    """Serialize a Decimal for storage: exact, finite, no scientific notation."""
    if not value.is_finite():
        msg = f"money values must be finite, got {value}"
        raise ValueError(msg)
    return format(value, "f")  # fixed-point — never exponent notation


def text_to_decimal(text: str) -> Decimal:
    """Parse a stored decimal string back to Decimal (never through float)."""
    return Decimal(text)
