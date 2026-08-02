# S2 spec — Storage & migrations

## Purpose

SQLite storage layer (WAL, FK on), a versioned forward-only migrations runner,
the ARCHITECTURE §3 schema, online backup + tested restore, and healthcheck
integration (`db_integrity`, `migrations_current`). Postgres/Timescale
migration stays viable (portable DDL; time-series tables carry a `ts` column
usable as a hypertable partition key).

## Public interface

### `dexpaprika.storage.db`

- `db_path(settings) -> Path` — `<data_dir>/dexpaprika.db`.
- `connect(path) -> sqlite3.Connection` — applies per-connection PRAGMAs:
  `journal_mode=WAL`, `foreign_keys=ON`, `synchronous=NORMAL`,
  `busy_timeout=5000`, `row_factory=sqlite3.Row`. (WAL persists in the file;
  set anyway — idempotent.)
- Decimal discipline helpers: `decimal_to_text(Decimal) -> str` and
  `text_to_decimal(str) -> Decimal` — exact round-trip, never float
  (reference doc §6; TEXT chosen over BLOB for auditability/portability —
  Postgres NUMERIC casts directly from these strings).

### `dexpaprika.storage.migrations`

- Migrations are packaged SQL files `src/dexpaprika/storage/sql/NNNN_name.sql`
  (importlib.resources), applied in filename order, each inside a transaction.
- `schema_migrations(version INTEGER PK, name TEXT, applied_at TEXT)` records
  applied versions.
- `migrate(conn) -> list[str]` — applies pending, returns applied names;
  re-run is a no-op (idempotent). A failing migration rolls back atomically:
  no partial DDL, no schema_migrations row, DB still usable.
- `pending(conn) -> list[str]`, `current_version(conn) -> int`.
- **Forward-only** (ADR): down-migrations are unsafe with data and unneeded
  with git-versioned SQL + backups; "down" = restore from backup.

### Schema v1 (`0001_initial.sql`) — ARCHITECTURE §3

Tables: `providers`, `provider_endpoint_costs`, `api_call_log`, `snapshots`,
`positions`, `position_events`, `hedge_state`, `orders`, `alerts_log`,
`audit_log`. Conventions: INTEGER PRIMARY KEY ids; `ts`/`*_at` TEXT ISO-8601
UTC; money/qty TEXT (Decimal strings); FKs with ON DELETE restrictions;
indexes on the query paths (provider+ts, position+ts, snapshot lookups).
`audit_log` is append-only by convention (no UPDATE/DELETE anywhere in code).
Wallet registry STAYS in wallets.json (S1 ADR) — migrating it now would churn
a verified section for no functional gain; revisit if multi-writer needs appear.

### `dexpaprika.storage.backup`

- `create_backup(conn, backup_dir) -> Path` — SQLite online backup API to
  `<data_dir>/backups/dexpaprika-<UTC timestamp>.db`; verifies the copy with
  `PRAGMA integrity_check` before returning; prunes to the newest 7 (config
  later if needed).
- `restore_backup(backup_path, db_path) -> None` — verifies backup integrity
  FIRST, moves the current DB aside to `<db>.pre-restore` (never destroys
  state), then replaces. Refuses if the backup fails integrity.

### CLI

```
dexpaprika db status   --json   # version, pending, integrity, path, size
dexpaprika db migrate  --json   # apply pending (idempotent)
dexpaprika db backup   --json   # create verified backup
dexpaprika db restore --from <path> --json   # verified restore
```

Exit 1 with `{"error": ...}` on failure. `db restore` without `--from` picks
the newest backup.

### Healthcheck

- `db_integrity`: `PRAGMA integrity_check` == ok AND `PRAGMA foreign_key_check`
  empty → "ok". A missing DB file reports fail with the fix (`db migrate`).
- `migrations_current`: no pending migrations → "ok".
- Remaining checks stay `not-implemented`; overall exit still 3 until all pass.

## Error cases

- Corrupt DB → integrity fail surfaces in healthcheck + `db status`; restore
  path documented in RUNBOOK.
- Failing migration → rollback, clear error naming the file; DB usable.
- Restore with missing/corrupt backup → refused, current DB untouched.
- Concurrent access → WAL + busy_timeout; single-writer convention documented.

## Standards obligations

- Decimal-as-TEXT everywhere money appears; parse from strings (§1).
- Idempotent, resumable operations (§2): migrate/backup re-runnable; restore
  never destroys the only copy.
- Writes confined to data_dir (§3). Parameterized SQL only.
- Coverage ≥90% on storage core (migrations/backup/db helpers).

## Probe gate (Step 2b)

N/A — no external API/on-chain reads; SQLite is local. Reference-doc patterns
(WAL persistence, backup API, PRAGMA set) are exercised directly by tests.

## References read (Step 2)

`reference\sqlite--best-practices.md` (WAL, PRAGMAs, backup API, money
storage, index left-to-right rule — all adopted);
`reference\timescaledb--api-reference--lp-tracker.md` (hypertables need a
TIMESTAMPTZ partition column → every time-series table has `ts` TEXT ISO
column; DDL kept portable); defi-portfolio best practices §8 (WAL/DECIMAL —
adopted at S0 research); REFERENCE_INDEX §0/§0.1 re-read (no storage impact).
