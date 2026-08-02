"""Postgres/Timescale dialect translation (S11) — pure, gate-tested."""

from __future__ import annotations

from dexpaprika.storage.migrations import packaged_migrations
from dexpaprika.storage.pgdialect import (
    HYPERTABLES,
    hypertable_ddl,
    translate_statement,
    translated_migrations,
)


def _all_translated() -> list[str]:
    return [
        statement
        for _version, _name, statements in translated_migrations()
        for statement in statements
    ]


class TestTranslation:
    def test_integer_primary_key_becomes_bigserial(self) -> None:
        out = translate_statement("CREATE TABLE t (id INTEGER PRIMARY KEY, x TEXT)")
        assert "BIGSERIAL PRIMARY KEY" in out
        assert "INTEGER PRIMARY KEY" not in out

    def test_ts_columns_become_timestamptz(self) -> None:
        out = translate_statement("CREATE TABLE t (ts TEXT NOT NULL, as_of TEXT NOT NULL)")
        assert "ts TIMESTAMPTZ NOT NULL" in out
        assert "as_of TEXT NOT NULL" in out  # only time-partition columns convert

    def test_ts_start_and_ts_end_convert(self) -> None:
        out = translate_statement("CREATE TABLE t (ts_start TEXT NOT NULL, ts_end TEXT)")
        assert "ts_start TIMESTAMPTZ NOT NULL" in out
        assert "ts_end TIMESTAMPTZ" in out

    def test_interval_identifier_is_quoted(self) -> None:
        """`interval` is a Postgres type keyword — quote it as an identifier."""
        out = translate_statement("CREATE TABLE ohlcv (interval TEXT NOT NULL)")
        assert '"interval" TEXT NOT NULL' in out

    def test_ordinary_statements_pass_through(self) -> None:
        sql = "CREATE INDEX idx_x ON t (a, b DESC)"
        assert translate_statement(sql) == sql


class TestTranslatedMigrations:
    def test_every_packaged_migration_translates_in_order(self) -> None:
        translated = translated_migrations()
        versions = [version for version, _name, _stmts in translated]
        assert versions == sorted(packaged_migrations())
        for _version, _name, statements in translated:
            assert statements
            for statement in statements:
                assert ";" not in statement  # still one statement per entry
                assert "INTEGER PRIMARY KEY" not in statement

    def test_no_bare_interval_identifier_survives(self) -> None:
        for statement in _all_translated():
            for word in statement.replace(",", " ").replace("(", " ").split():
                assert word != "interval", statement


class TestHypertables:
    def test_exactly_the_appendonly_tables(self) -> None:
        assert set(HYPERTABLES) == {"api_call_log", "pool_metrics", "ohlcv"}

    def test_time_columns_and_chunks(self) -> None:
        assert HYPERTABLES["api_call_log"][0] == "ts"
        assert HYPERTABLES["pool_metrics"][0] == "ts"
        assert HYPERTABLES["ohlcv"][0] == "ts_start"

    def test_ddl_drops_surrogate_id_then_converts(self) -> None:
        ddl = hypertable_ddl()
        for table, (column, chunk) in HYPERTABLES.items():
            drop = f"ALTER TABLE {table} DROP COLUMN id"
            assert drop in ddl
            create = next(s for s in ddl if f"create_hypertable('{table}'" in s)
            # TimescaleDB 2.17 generalized API (rehearsal-verified): chunk
            # interval rides INSIDE by_range().
            assert f"by_range('{column}', INTERVAL '{chunk}')" in create
            # DROP must precede conversion (hypertables convert empty tables).
            assert ddl.index(drop) < ddl.index(create)

    def test_hypertable_tables_are_not_fk_targets(self) -> None:
        """Referenced tables (snapshots, positions, …) must stay regular."""
        all_sql = " ".join(_all_translated())
        for table in HYPERTABLES:
            assert f"REFERENCES {table}" not in all_sql
