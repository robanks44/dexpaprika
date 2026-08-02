"""Timescale migration rehearsal (S11) — EXECUTED against a disposable DB.

Usage:
    docker run -d --name ts-rehearsal --network host \
        -e POSTGRES_PASSWORD=rehearsal timescale/timescaledb:2.17.2-pg16
    uv run --group pg python scripts/pg_rehearsal.py \
        "postgresql://postgres:rehearsal@127.0.0.1:5432/postgres"
    docker rm -f ts-rehearsal

Applies the SAME packaged migrations (pgdialect-translated), converts the
append-only time-series tables to hypertables, inserts one Decimal-string
row per hypertable, and verifies. Prints a JSON report (dump it under
probes/out/s11/ when run as the section probe).
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

import psycopg

sys.path.insert(0, "src")

from dexpaprika.storage.pgdialect import (
    HYPERTABLES,
    hypertable_ddl,
    translated_migrations,
)

_INSERTS = {
    "api_call_log": (
        "INSERT INTO api_call_log (ts, provider_id, endpoint, credits) VALUES (%s, 1, 'e', 1)"
    ),
    "pool_metrics": (
        "INSERT INTO pool_metrics (ts, network, pool_address, source, price_usd)"
        " VALUES (%s, 'base', '0xpool', 'rehearsal', '1845.123456789012345678')"
    ),
    "ohlcv": (
        'INSERT INTO ohlcv (network, pool_address, "interval", ts_start, open, high,'
        " low, close, source, as_of)"
        " VALUES ('base', '0xpool', '24h', %s, '1', '2', '0.5', '1.5', 'rehearsal', 'now')"
    ),
}


def main(dsn: str) -> int:
    now = datetime.now(UTC)
    report: dict[str, object] = {"rehearsed_at": now.isoformat(), "migrations": []}
    with psycopg.connect(dsn, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        conn.commit()

        applied = []
        for version, name, statements in translated_migrations():
            for statement in statements:
                cur.execute(statement)  # type: ignore[arg-type]  # dynamic but repo-controlled DDL
            applied.append(f"{version:04d}_{name} ({len(statements)} statements)")
        report["migrations"] = applied

        # api_call_log needs a provider row (FK) before its insert.
        cur.execute(
            "INSERT INTO providers (name, base_url, rate_limit, rate_period)"
            " VALUES ('rehearsal', 'https://x', 1, 'minute')"
        )

        for statement in hypertable_ddl():
            cur.execute(statement)  # type: ignore[arg-type]  # dynamic but repo-controlled DDL

        cur.execute("SELECT hypertable_name FROM timescaledb_information.hypertables")
        hypertables = sorted(row[0] for row in cur.fetchall())
        report["hypertables"] = hypertables

        counts: dict[str, int] = {}
        for table, insert_sql in _INSERTS.items():
            cur.execute(insert_sql, (now,))  # type: ignore[arg-type]  # repo-controlled SQL
            cur.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 — table from repo constant
            counts[table] = int(cur.fetchone()[0])  # type: ignore[index]  # COUNT always returns one row
        report["row_counts"] = counts
        conn.commit()

    ok = set(HYPERTABLES) <= set(hypertables) and all(c == 1 for c in counts.values())
    report["verdict"] = "PASS" if ok else "FAIL"
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write("usage: pg_rehearsal.py <postgres-dsn>\n")
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
