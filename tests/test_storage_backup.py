"""Backup & restore — online backup, verified restore, no destructive paths."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from dexpaprika.storage.backup import BackupError, create_backup, restore_backup
from dexpaprika.storage.db import connect
from dexpaprika.storage.migrations import migrate


@pytest.fixture
def db(tmp_path: Path) -> Iterator[tuple[Path, sqlite3.Connection]]:
    path = tmp_path / "data" / "dexpaprika.db"
    path.parent.mkdir(parents=True)
    conn = connect(path)
    migrate(conn)
    conn.execute(
        "INSERT INTO providers (name, base_url, rate_limit, rate_period)"
        " VALUES ('dexpaprika', 'https://api.dexpaprika.com', 30, 'minute')"
    )
    conn.commit()
    yield path, conn
    conn.close()


def test_backup_round_trip(db: tuple[Path, sqlite3.Connection], tmp_path: Path) -> None:
    path, conn = db
    backup_dir = tmp_path / "data" / "backups"
    backup_path = create_backup(conn, backup_dir)
    assert backup_path.exists()

    # Damage the live DB state (delete the row), then restore.
    conn.execute("DELETE FROM providers")
    conn.commit()
    conn.close()

    restore_backup(backup_path, path)
    restored = connect(path)
    try:
        count = restored.execute("SELECT COUNT(*) AS n FROM providers").fetchone()["n"]
        assert count == 1
    finally:
        restored.close()


def test_backup_is_valid_while_db_in_use(
    db: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    path, conn = db
    backup_path = create_backup(conn, tmp_path / "b")
    check = sqlite3.connect(backup_path)
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()


def test_restore_refuses_corrupt_backup(
    db: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    path, conn = db
    fake = tmp_path / "corrupt.db"
    fake.write_bytes(b"this is not a sqlite database at all")
    with pytest.raises(BackupError, match="integrity|not a database"):
        restore_backup(fake, path)
    # Live DB untouched.
    assert conn.execute("SELECT COUNT(*) AS n FROM providers").fetchone()["n"] == 1


def test_restore_keeps_pre_restore_copy(
    db: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    path, conn = db
    backup_path = create_backup(conn, tmp_path / "b")
    conn.close()
    restore_backup(backup_path, path)
    assert path.with_name(path.name + ".pre-restore").exists()


def test_backup_pruning_keeps_newest(db: tuple[Path, sqlite3.Connection], tmp_path: Path) -> None:
    path, conn = db
    backup_dir = tmp_path / "b"
    paths = [create_backup(conn, backup_dir, keep=3) for _ in range(5)]
    remaining = sorted(backup_dir.glob("*.db"))
    assert len(remaining) == 3
    assert paths[-1] in remaining
