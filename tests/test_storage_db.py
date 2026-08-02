"""Storage connection layer — PRAGMAs, Decimal discipline."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dexpaprika.storage.db import connect, decimal_to_text, text_to_decimal


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "t.db")
    yield connection
    connection.close()


def test_wal_mode_enabled(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_foreign_keys_on(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_synchronous_normal(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL


def test_row_factory_named_access(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT 1 AS one").fetchone()
    assert row["one"] == 1


def test_wal_persists_across_reconnect(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    connect(path).close()
    plain = sqlite3.connect(path)
    try:
        assert plain.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        plain.close()


class TestDecimalDiscipline:
    def test_round_trip_exact(self) -> None:
        value = Decimal("13155.762269646219571243906932")
        assert text_to_decimal(decimal_to_text(value)) == value

    def test_never_scientific_notation(self) -> None:
        # SQL text sorting/reading must not meet '1E+2'-style strings.
        assert "E" not in decimal_to_text(Decimal("1E+2")).upper()

    def test_rejects_nan_and_infinity(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            decimal_to_text(Decimal("NaN"))
        with pytest.raises(ValueError, match="finite"):
            decimal_to_text(Decimal("Infinity"))

    @given(
        st.decimals(
            allow_nan=False,
            allow_infinity=False,
            min_value=Decimal("-1e30"),
            max_value=Decimal("1e30"),
        )
    )
    def test_round_trip_property(self, value: Decimal) -> None:
        assert text_to_decimal(decimal_to_text(value)) == value
