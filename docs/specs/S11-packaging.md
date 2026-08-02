# S11 spec — Cloud packaging

## Purpose

One artifact that runs unchanged locally and in a container (§6), with:
compose running the scheduler-driven CLI, the TimescaleDB/Postgres
migration path REHEARSED against a disposable database (not just
documented), the secret-provider swap exercised, a CycloneDX SBOM on the
release artifact (§3), and deployment notes (VPS unlocks SSE streaming).

## Reference gate

- `ENGINEERING_STANDARDS.md` §6 (multi-stage non-root Dockerfile — S0
  shipped it; compose for parity; externalized scheduling; documented
  Timescale path) and §3 (CycloneDX SBOM on release).
- `reference\timescaledb--api-reference--lp-tracker.md`: hypertables via
  `create_hypertable(by_range(time_col))` on an EMPTY table; TIMESTAMPTZ
  time column; chunk interval 1h–1d for this data volume; the
  hypertable-PK rule (every unique index must include the partition
  column) is the known migration gotcha the rehearsal must face.
- `reference\python-scheduling--playbook--windows.md` **Option B**: a
  persistent process (the VPS/container case — exactly this section)
  uses APScheduler 3.x with `max_instances=1`, `coalesce=True`,
  `misfire_grace_time` — pin the major version.

## Environment probe (Step 2b)

Sandbox verified live: dockerd starts (`--iptables=false --bridge=none`;
containers use `--network host`), Docker Hub reachable (python:3.13-slim
pulled), compose v5 plugin present. Rehearsal DB: `timescale/timescaledb`
(pg16) container. Rehearsal output dumped to `probes/out/s11/`.

## Deliverables

### 1. Scheduler entrypoint (`dexpaprika scheduler`)

The container has no Task Scheduler — the playbook's Option B applies.
New dependency: `apscheduler>=3.11,<4` (major pinned per playbook).

- `dexpaprika scheduler jobs --json` — the configured jobs (offline,
  testable): `snapshot` hourly (cron minute=0), `alerts-check` every 5
  min, `db-backup` daily 03:10 UTC — each `max_instances=1`,
  `coalesce=True`, `misfire_grace_time=120s` (sleep/lag-safe per the
  playbook). Cadences configurable: `DEXPAPRIKA_SCHEDULER_SNAPSHOT_*`,
  `..._ALERTS_MINUTES`, `..._BACKUP_*`.
- `dexpaprika scheduler run` — BlockingScheduler driving the SAME CLI
  mains in-process (`main(["snapshot", "--json"])` …); each run's exit
  code is logged to stdout as one JSON line (container logs = health
  log, same exit-code honesty as schtasks history).
- Windows keeps schtasks (S8) — `scheduler run` is the container path.

### 2. Compose (`compose.yaml`)

One `scheduler` service: `build: .`, `command: ["scheduler", "run"]`,
`env_file: .env` (+ `DEXPAPRIKA_SECRET_*` from environment — the
secret-provider swap in action: keyring on Windows, env in containers),
named volume `dexpaprika-data:/data`, `restart: unless-stopped`,
`read_only: true` rootfs with tmpfs `/tmp` (defense in depth; §6
non-root user already in the image). One-off ops run through the same
image: `docker compose run --rm scheduler healthcheck --json`.

### 3. Timescale migration rehearsal (executed, not documented)

- `src/dexpaprika/storage/pgdialect.py` (gate-tested, pure): translates
  the packaged SQLite migrations to Postgres —
  `INTEGER PRIMARY KEY` → `BIGSERIAL PRIMARY KEY`; `ts TEXT` →
  `ts TIMESTAMPTZ` (ISO-8601 strings insert cleanly); emits hypertable
  conversion DDL for the append-only time-series tables
  `api_call_log`, `pool_metrics`, `ohlcv` — for those, the surrogate
  `id` PK is REPLACED (hypertable rule: unique indexes must include
  `ts`): api_call_log keeps no PK (append-only log, index stays),
  pool_metrics/ohlcv get their existing UNIQUE(ts,…) keys as PK.
  Referenced tables (`snapshots`, `position_events`, …) stay regular —
  their `id` is a foreign-key target, which hypertables can't be.
- `scripts/pg_rehearsal.py` (network/db, out of gate): applies the
  translated migrations + hypertable DDL to a DSN, inserts one
  Decimal-string row per hypertable, verifies counts +
  `timescaledb_information.hypertables`, prints a JSON report.
- Rehearsal is RUN (build + verifier) against a disposable
  `timescale/timescaledb` pg16 container; report → `probes/out/s11/`.
- New optional dependency group `pg`: `psycopg[binary]>=3.2` (not in the
  runtime image).

### 4. SBOM + release artifact

- Dev dep `cyclonedx-bom`; `make sbom` → CycloneDX JSON for the FROZEN
  runtime dependency set (`uv export --frozen --no-dev` →
  `cyclonedx-py requirements`) at `dist/sbom.cdx.json`.
- `make release` → `uv build` (wheel+sdist) + `make sbom`: the release
  artifact ships with its SBOM (§3).

### 5. RUNBOOK — Deployment (S11)

Build/run/compose commands, env-secret pattern (`DEXPAPRIKA_SECRET_*` —
never in the image or compose file), volume backup note, the rehearsal
command, and the VPS note: a persistent VPS unlocks SSE streaming
(e.g. ntfy subscribe / future streaming sources) that scheduled runs
cannot hold open.

## Test focus (offline gate)

- pgdialect: BIGSERIAL/TIMESTAMPTZ translation, hypertable DDL set
  (exact three tables), id-PK replacement rules, idempotent on non-ts
  tables, every emitted statement still one-per-`;`.
- scheduler: jobs registered with the exact playbook knobs
  (max_instances/coalesce/misfire), cadences from Settings env, job
  funcs invoke `cli.main` with the right argv (monkeypatched), nonzero
  exit logged not raised.
- compose/Dockerfile integrity: compose file parses (`docker compose
  config` is verifier-side; the gate asserts the file's required keys
  via yaml parse) and RUNBOOK deployment commands parse per the S10
  doc-integrity gate.

## Done criteria (verifier)

- Gate + audit green (offline suite untouched by docker).
- `docker build` succeeds; SAME artifact green in-container: bootstrap +
  live snapshot of the real wallet + `healthcheck` exit 0 INSIDE the
  container with env-backend secrets (secret swap exercised).
- `docker compose config` validates; `scheduler jobs --json` shows the
  three jobs in-container.
- `scripts/pg_rehearsal.py` against a disposable timescaledb container:
  all migrations applied, 3 hypertables live, Decimal-string inserts
  verified.
- `make release` produces wheel + `dist/sbom.cdx.json` (valid CycloneDX
  with the runtime components).
