"""Online backup + verified restore (ENGINEERING_STANDARDS §2).

Backups use SQLite's online backup API (safe while the DB is in use) and are
integrity-checked before they count. Restore verifies the backup FIRST and
always moves the current database aside (``.pre-restore``) rather than
destroying it — there is no code path that deletes the only copy of state.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

BACKUP_PREFIX = "dexpaprika-"


class BackupError(Exception):
    """Backup or restore failed; the live database was not harmed."""


def _integrity_ok(path: Path) -> str | None:
    """Return None if the file passes integrity_check, else the failure text."""
    try:
        conn = sqlite3.connect(path)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return str(exc)
    return None if result == "ok" else str(result)


def create_backup(conn: sqlite3.Connection, backup_dir: Path, keep: int = 7) -> Path:
    """Back up the live connection into ``backup_dir``; verify; prune to ``keep``."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S-%f")
    destination = backup_dir / f"{BACKUP_PREFIX}{stamp}.db"
    target = sqlite3.connect(destination)
    try:
        conn.backup(target)
    finally:
        target.close()
    failure = _integrity_ok(destination)
    if failure is not None:
        destination.unlink(missing_ok=True)
        msg = f"backup failed integrity check and was discarded: {failure}"
        raise BackupError(msg)
    # Prune oldest beyond `keep` (timestamped names sort chronologically).
    backups = sorted(backup_dir.glob(f"{BACKUP_PREFIX}*.db"))
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink()
    return destination


def latest_backup(backup_dir: Path) -> Path | None:
    """Newest backup in the directory, or None."""
    backups = sorted(backup_dir.glob(f"{BACKUP_PREFIX}*.db"))
    return backups[-1] if backups else None


def restore_backup(backup_path: Path, db_path: Path) -> None:
    """Verified restore: refuse bad backups; keep the old DB as .pre-restore."""
    if not backup_path.exists():
        msg = f"backup not found: {backup_path}"
        raise BackupError(msg)
    failure = _integrity_ok(backup_path)
    if failure is not None:
        msg = f"refusing restore — backup fails integrity check ({failure}): {backup_path}"
        raise BackupError(msg)
    if db_path.exists():
        db_path.replace(db_path.with_name(db_path.name + ".pre-restore"))
    # Stale WAL/SHM from the old database must not pollute the restored file.
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        sidecar.unlink(missing_ok=True)
    shutil.copy2(backup_path, db_path)
