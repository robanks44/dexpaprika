"""SQLite storage layer: connections, migrations, backup (S2)."""

from dexpaprika.storage.backup import BackupError, create_backup, restore_backup
from dexpaprika.storage.db import connect, db_path, decimal_to_text, text_to_decimal
from dexpaprika.storage.migrations import (
    MigrationError,
    current_version,
    migrate,
    pending,
)

__all__ = [
    "BackupError",
    "MigrationError",
    "connect",
    "create_backup",
    "current_version",
    "db_path",
    "decimal_to_text",
    "migrate",
    "pending",
    "restore_backup",
    "text_to_decimal",
]
