# RUNBOOK.md — operating dexpaprika

Written so a fresh Claude session can operate the system from this file alone.
Every command supports `--json` (use it — you are the operator). Exit codes:
0 ok, 1 operational failure (JSON `{"error": ...}` explains what to fix),
2 usage error, 3 degraded (healthcheck only).

## First reads in any session (bootstrap order)

```
dexpaprika healthcheck --json   # 1. trustworthy right now? exit 0 = yes
dexpaprika report --json        # 2. portfolio by group, with as_of
dexpaprika hedge status --json  # 3. coverage / quadrant / flags
dexpaprika alerts log --json    # 4. what fired lately, was it delivered
dexpaprika quota --json         # 5. remaining API headroom
dexpaprika status --json        # (anytime) config + wallet summary
```

Healthcheck failing = fix that FIRST (each `fail: ...` says how; reference
table below). All nine checks are real as of S10; `upstream_reachability`
and `clock_sanity` need network — the rest are offline.

### Healthcheck reference (S10)

| check | verifies | on fail |
|---|---|---|
| `db_integrity` | PRAGMA integrity + FK check | `dexpaprika db restore` from the newest verified backup |
| `migrations_current` | schema at packaged head | `dexpaprika db migrate` |
| `upstream_reachability` | one cheap live call per upstream (Base/Arbitrum RPC, GMX, DexPaprika; ntfy excluded — `alerts test` covers it) | named upstream is down: check connectivity, wait out the ring/breaker, or extend the URL rings via env |
| `secrets_present` | `ntfy_topic` resolvable | store it in keyring (service `dexpaprika`) or `DEXPAPRIKA_SECRET_NTFY_TOPIC` |
| `clock_sanity` | local UTC vs Base block time (≤5 min skew) | fix the system clock — staleness/cooldown/quota windows lie until then |
| `last_snapshot_age` | newest snapshot ≤ 90 min old | scheduled recorder gapped: check the Task Scheduler task, run `dexpaprika snapshot` |
| `data_dir_writable` | write probe in `DEXPAPRIKA_DATA_DIR` | fix path/permissions |
| `repo_state` | git checkout clean (installed wheel = ok) | commit or stash — never operate on unverified code |
| `operational_state` | mode (read-only; S9 gated), limits, live short exposure vs `max_position_usd` | exposure over a configured limit: do NOT act; resolve sizing/limits first |

## Configuration (S1)

- All config via `DEXPAPRIKA_*` env vars (optional `.env` for non-secret local
  values; see `.env.example`). Data lives only under `DEXPAPRIKA_DATA_DIR`
  (default `./data`).
- Secrets: NEVER in `.env` or config. Local machine → OS keyring, service
  `dexpaprika`, names: `github_pat`, `ntfy_topic`, `dexpaprika_api_key`,
  `krystal_api_key`, `coinstats_api_key`. Cloud/CI → `DEXPAPRIKA_SECRET_<NAME>`
  env vars. Backend selection: `DEXPAPRIKA_SECRET_BACKEND=auto|keyring|env`
  (auto = keyring then env).
- To store a secret on Windows (one-time, Richard or Claude via desktop):
  `uv run python -c "import keyring; keyring.set_password('dexpaprika','ntfy_topic','<value>')"`

## Wallet registry (S1)

```
dexpaprika wallets list --json
dexpaprika wallets add --chain evm --address 0xYourAddress [--label main] --json
dexpaprika wallets remove --address 0xYourAddress --json   # or select by --label
dexpaprika wallets exclude --label <name> --json   # keep registered, stop tracking
dexpaprika wallets include --label <name> --json   # resume tracking
```

- Addresses are validated and normalized at entry (EVM → EIP-55; BTC bech32 →
  lowercase; mainnet only). Downstream sections read ONLY `included` wallets.
- Registry file: `<data_dir>/wallets.json` (atomic writes).

## Failure modes & recovery

| Symptom | Meaning | Recovery |
|---|---|---|
| `wallets add` exit 1 "fails EIP-55 checksum" | mistyped mixed-case address | re-copy the address, or paste all-lowercase to let normalization checksum it |
| `wallets add` exit 1 "duplicate" | already registered | `wallets list --json` to see it; use include/exclude instead |
| exit 1 "corrupt wallet registry at <path>" | wallets.json unparseable | file is NOT auto-overwritten; restore it from backup/git or fix by hand, retry |
| healthcheck `secrets_present: fail` | ntfy topic not stored | store `ntfy_topic` in keyring (service `dexpaprika`) or set `DEXPAPRIKA_SECRET_NTFY_TOPIC` |
| healthcheck `data_dir_writable: fail` | bad path/permissions | check `DEXPAPRIKA_DATA_DIR`, create dir or fix perms |
| keyring silently returns nothing on Linux | no keyring backend in VM | expected; use `DEXPAPRIKA_SECRET_*` env vars (auto backend falls through) |

## Database (S2)

```
dexpaprika db status --json    # exists, pending migrations, integrity, size
dexpaprika db migrate --json   # apply pending (idempotent; safe to re-run)
dexpaprika db backup --json    # verified online backup -> <data_dir>/backups/ (keeps 7)
dexpaprika db restore --json [--from <path>]   # verified restore; newest backup default
```

- DB file: `<data_dir>/dexpaprika.db` (WAL mode — `-wal`/`-shm` sidecars are normal).
- Restore never destroys state: the old DB is kept as `dexpaprika.db.pre-restore`.
- Migrations are forward-only; "rolling back" = `db restore` from a backup.

| Symptom | Meaning | Recovery |
|---|---|---|
| healthcheck `db_integrity: fail ... missing` | DB not created yet | `dexpaprika db migrate` |
| healthcheck `db_integrity: fail ... integrity_check` | corruption | `dexpaprika db restore` (newest backup), then `db status` |
| healthcheck `migrations_current: fail` | new code, old schema | `dexpaprika db migrate` |
| `db migrate` exit 1 "rolled back" | bad migration file | DB unchanged and usable; fix the SQL, rerun |
| `db restore` exit 1 "refusing restore" | backup corrupt | pick an older backup with `--from` |

## Provider quotas (S2.5)

```
dexpaprika quota --json                  # all providers: window + month credits + pct_used
dexpaprika quota --provider coinstats --json
```

- Every upstream call (S3+ clients) is checked and logged against `api_call_log`,
  so spend survives restarts and is shared across processes.
- `window_mode: credits` (Hyperliquid) means the per-minute limit counts WEIGHT,
  not calls. CoinStats `wallet/defi*` costs 400 credits — scope to one chain, never "all".
- A `credit-budget` denial cannot be waited out — it clears next UTC month.
  Raise the budget in the `providers` table only if the paid plan actually changed.

## Market data — DexPaprika (S3)

```
dexpaprika market pool  --network base --address <pool> [--record] --json
dexpaprika market ohlcv --network base --address <pool> --start YYYY-MM-DD \
                        [--interval 24h] [--limit N] [--record] --json
```

- Requires a migrated DB (quota tracking lives there). All calls are
  quota-gated at 30/min and logged — check spend with `dexpaprika quota`.
- ROLE BOUNDARY: DexPaprika prices are history/volume ONLY (~2% skew verified).
  Never use them for range/edge or hedge math — that reads the pool contract.
- `fee` is null for SlipStream pools — expected, not an error.
- "circuit open for 'dexpaprika'" = 5 consecutive failed calls; wait ~60s,
  it self-heals on the next successful probe.

## GMX hedge leg (S4)

```
dexpaprika gmx positions [--address 0x...] [--record] --json
```

- `--address` defaults to the single INCLUDED EVM wallet in the registry.
- All numbers are exact Decimal strings (may carry trailing zeros).
- `positions: []` with a note is a VALID state — no open positions; a closed
  or liquidated position looks exactly like this. S7/S8 alert on the change.
- Related SL order shows `order_kind: stop-loss-decrease`, scaled
  `trigger_price`, `is_full_close` instead of a fake size number.
- Peers rotate automatically; "all GMX peers failed" names both with reasons.

## On-chain snapshots (S4.5)

```
dexpaprika chain snapshot [--chain base|arbitrum|all] --json
```

- One pinned Multicall3 batch per chain, recorded in `snapshots` with the
  block number. Tripwires verify the node answered at the pinned block AND
  the right chain; a "lagging load-balanced node" error means the snapshot
  was discarded — just rerun.
- Arbitrum note: block numbers are the LARGE L2 numbers (~490M). If a
  future change shows ~25M there, the L1-block bug has crept back in.
- RPC endpoint rings are config (`DEXPAPRIKA_*_RPC_URLS`); a dying free
  endpoint is a config edit, not a code change.

## LP positions (S5)

```
dexpaprika lp snapshot [--address 0x...] [--record] --json
```

- Custody-aware: finds wallet-held, Sickle-held (vfat.io), and gauge-staked
  positions; every position row carries `custody` + `custodian`.
- All reads at ONE pinned block (`block_number` in the output). Amounts are
  exact Decimals; `in_range` + `pool_tick` drive range alerts (S7/S8).
- `pool_unresolved` or a decimals warning means the position was recorded
  but NOT valued — extend the registry rather than trusting a guess.
- A sickle whose `owner()` isn't the wallet is EXCLUDED (warning says so).

## Portfolio snapshot & report (S6)

```
dexpaprika snapshot [--kind lp|hedge|defi|holdings|all] [--address 0x...] --json
dexpaprika report --json
```

- `snapshot` is THE hourly job: records every group, derives lifecycle
  events (open/modify/full_close), reconciles closures, one pinned block
  per chain. Safe to re-run — observations append, nothing duplicates.
- `report` is the agent's portfolio read: three groups (lp_hedge / defi /
  holdings) with as_of + source per entry, lp_value + defi_net totals.
- Holdings cover EVM (Base native + tokens) AND native BTC (S5.5): register
  a BTC wallet with `--chain btc` and `snapshot --kind holdings` reads its
  balance via Esplora (blockstream primary, mempool.space fallback; peers
  configurable via `DEXPAPRIKA_BTC_ESPLORA_PEERS`). The BTC snapshot row
  carries `chain='bitcoin'` with no block (timestamped off-chain source).
  Esplora failures surface as nonzero snapshot exits + the staleness alert.
- A GMX `full_close` event appearing = the short is GONE (SL fired or
  closed) — this is the alertable state S8 watches.
- Holdings show amounts only (pricing joins in S7/S8).

## Recorder — full-variable service (S12a)

```
dexpaprika recorder cycle  [--kind lp|hedge|defi|holdings|all] [--address 0x...] --json
dexpaprika recorder run    [--kind ...] [--lp-interval S] [--hedge-interval S] [--max-cycles N] --json
dexpaprika recorder status --json
```

- `recorder cycle` is one recording cycle — the SAME rows as `snapshot`, and the
  correctness fallback: a series of scheduled `cycle` calls == one `run`. It is
  resilient (per-source isolation): a failed source is stamped not-ok and heart-
  beated, never aborting the others, so it exits 0 with an `ok` flag. (`snapshot`
  keeps the fail-hard contract — any failed source → nonzero exit — use it when
  you want all-or-nothing.)
- `recorder run` is the long-running service (foreground; a Windows service/NSSM
  or scheduled-at-logon task wraps it). Each source runs on its own cadence
  (`--lp-interval`, `--hedge-interval`); a failed source retries on capped
  exponential backoff. `--max-cycles` bounds it for test and smoke runs.
- `recorder status` shows the last cycle per source with staleness seconds +
  heartbeat age (from the `recorder_heartbeat` table). Staleness is honest — a
  failed source keeps its previous stamp, flagged not-ok.
- Full-variable capture (S12a): LP state also carries both token USD prices and
  the pool's 24h volume (DexPaprika, null-with-reason when absent); the hedge
  state carries the SL order size alongside its trigger. Storage is RAW only —
  derived metrics are a read-time concern (the S12b dashboard).

## Dashboard — live view + SSE (S12b)

```
dexpaprika dashboard serve  [--host 127.0.0.1] [--port 8787]
dexpaprika dashboard export [--out FILE]
```

- `dashboard serve` runs a LOCAL, READ-ONLY server on 127.0.0.1. It reads only the
  SQLite DB the recorder writes and **never calls upstream** — a viewer refresh
  cannot trigger an API call. Live updates push to the browser via SSE (`/events`),
  triggered by a local DB watch (new `snapshots` row), so one local feed serves N
  viewers. Routes: `/` (dashboard), `/api/latest`, `/api/history`, `/api/derived`,
  `/events`, `/static/echarts.min.js`. Typically launched beside `recorder run`.
- Panels: derived-metric KPI tiles + gauges (distance-to-SL, distance-to-floor,
  in-range position — reusing `hedge.engine.analyze`), time-series charts (price,
  hedge size, pool volume, funding), and per-source raw tables. Every panel shows
  its source's staleness; a dead source LOOKS dead (badge flips past ~5 min).
- `dashboard export` writes ONE self-contained HTML snapshot (`<data_dir>/dashboard.html`
  by default): latest + derived + histories inlined, ECharts inlined — opens offline
  with charts intact, zero external requests. Use it for sharing/archival.
- Charts use Apache ECharts vendored locally (`dashboard/static/echarts.min.js.gz`,
  Apache-2.0) — no CDN, zero new Python dependency.

## External watchdog + daily digest (S13)

```
dexpaprika watchdog heartbeat [--state auto|ok|fail|start]
dexpaprika watchdog digest    [--dry-run]
dexpaprika watchdog status
```

- **Setup (operator, one-time):** create a check on an OFF-MACHINE dead-man's switch
  (healthchecks.io free tier or equivalent), then store its ping URL as the secret
  `heartbeat_url` — OS keyring (service `dexpaprika`) or `DEXPAPRIKA_SECRET_HEARTBEAT_URL`.
  **It MUST NOT live on the watched machine** — that's the whole point: a dead machine
  cannot alert itself, so the switch (elsewhere) raises the alert when pings stop.
- `watchdog heartbeat` (scheduled every `watchdog_heartbeat_minutes`, default 5): `auto`
  assesses recorder freshness, then pings `ok` when fresh, `fail` when stale. A dead machine
  sends nothing → the switch trips on silence. Set the switch's grace period comfortably above
  the ping cadence. Unconfigured URL → honest no-op (exit 3 degraded), never a fake success.
- `watchdog digest` (scheduled daily at `watchdog_digest_hour` UTC, default 13:00): builds an
  "all clear / attention" position summary (LP in-range, hedge coverage, distance-to-SL,
  net delta, funding, staleness) and sends it to ntfy — replacing the old once-daily manual
  check. It reports "all clear" ONLY over fresh, healthy data; stale/missing/out-of-range/
  near-SL downgrades it to "attention" (never a fabricated green). `--dry-run` prints without
  sending. Rebalance drift is shown as a line, not an all-clear downgrade.
- `watchdog status`: offline — is `heartbeat_url` configured?, plus the recorder freshness
  verdict. No ping.
- The digest is a SUMMARY, not a re-fire of S8 alerts; S8 still fires actionable per-rule
  alerts. The watchdog adds the external liveness guard + the daily all-clear.

## Delta-band rebalance strategy (S14 — auto-execute is OPT-IN)

```
dexpaprika strategy status                 # delta gap, target, every gate state (offline)
dexpaprika strategy rebalance [--arm]       # evaluate + (only if enabled + armed) auto-execute
```

- **North star:** decisions optimize for the greatest NET CAPITAL position (penalize fee
  churn, unhedged-delta losses, AND catastrophic failure). Resize the GMX short to track the
  LP's live ETH exposure, triggered by delta DRIFT, not price.
- **Ships DORMANT.** `auto_rebalance_enabled` defaults **False** → `strategy rebalance` is
  SHADOW/propose only, no matter what else is set. Live auto-execution needs BOTH the config
  flag on AND `--arm` (+ the S9 armed-state file). Capital-optimal rollout: **shadow → measure
  → tune → enable** — let it journal decisions, tune the band/interval/cost-floor from
  recorded data, THEN turn it on.
- **No S9 guard bypassed.** Auto-execute reuses the S9 pipeline (`execute_instruction`) with an
  auto-approving callback — kill-switch, armed-state, hard limits (max position, max daily
  adjustments, max delta per run), audit, post-condition verify, idempotency ALL still fire.
  A big gap is stepped in per-run-cap-sized increments (converges over cycles, never exceeds
  the cap). Every executed rebalance sends an ntfy notification.
- **Gates (all must pass to act):** band breached (`hedge_rebalance_band`, 7.5%), state fresh,
  `rebalance_min_interval_minutes` (60) since the last executed rebalance, gap ≥
  `rebalance_min_notional_usd` ($250 cost floor), under the daily/position caps. `strategy
  status` shows each gate; blocked decisions list which gate stopped them.
- **SL untouched** — the widened stop-loss stays as the backstop. Every decision (shadow +
  executed) is journaled to `rebalance_log` for net-capital attribution.
- **To enable live (when ready):** set `DEXPAPRIKA_AUTO_REBALANCE_ENABLED=true`, run
  `dexpaprika execute arm`, and let the scheduled `strategy-rebalance` job (opt-in, interval
  `strategy_rebalance_minutes`) run `strategy rebalance --arm`.

## Hedge analysis (S7 — read-only)

```
dexpaprika hedge status --json                     # from latest snapshots
dexpaprika hedge simulate [--price P | --curve N] --json
```

- Requires `snapshot --kind lp` (+ `hedge`) first. Missing short = `naked-lp`
  flag — THE alertable state after an SL stop-out.
- Fields follow the Insurance Policy strategy: quadrant (Q3 = profit zone,
  Q4 boundary = decision point), `break_even_short_size` (S*),
  `delta_matched_target_eth`, `rebalance_needed` (band 7.5% of max delta),
  `premium_if_sl_fires` (stop-out cost).
- Standing flags on the current book: `over-hedged` (fixed 7.04 short vs
  moving delta), `sl-correlated-with-top-exit`, `sl-below-q1q2-rule`
  (SL $1925 at ~63% vs the 75% rule) — strategy decisions, not bugs.
- `simulate --curve 9` renders the dual-curve P&L floor→ceiling.

## Alerts & reporting (S8)

```
dexpaprika alerts check [--dry-run] --json   # evaluate → record → deliver
dexpaprika alerts test --json                # one live test notification
dexpaprika alerts log [--limit N] --json     # firing history (audit)
```

- Channel: ntfy topic (secret `ntfy_topic` — keyring service `dexpaprika`
  or `DEXPAPRIKA_SECRET_NTFY_TOPIC`). The topic never appears in URLs,
  logs, errors, or `alerts_log` (JSON publish to `/`, endpoint label
  `publish`).
- Rules: `naked-lp` + `price-near-sl` (urgent); `near-band-edge`,
  `rebalance-needed`, `snapshot-stale` (>90 min), `quota-critical`
  (monthly credit budget ≥80%), `healthcheck-degraded` (high).
- Every firing lands in `alerts_log` BEFORE delivery — a dead channel
  exits 3 (degraded) with `delivered=0` + `ntfy_status`, never a lost
  record. Same rule re-fires only after the 60-min cooldown
  (`DEXPAPRIKA_ALERT_COOLDOWN_MINUTES`).
- An alert firing is the system WORKING: `alerts check` exits 0 when all
  deliveries succeed, 3 when delivery is degraded, 1 on failure — so Task
  Scheduler history doubles as a health log.

### Scheduled tasks (Windows Task Scheduler drives the CLI)

```bat
schtasks /Create /TN "dexpaprika\recorder" /SC HOURLY ^
  /TR "C:\path\venv\Scripts\dexpaprika.exe snapshot --json" /F
schtasks /Create /TN "dexpaprika\alerts" /SC MINUTE /MO 5 ^
  /TR "C:\path\venv\Scripts\dexpaprika.exe alerts check --json" /F
```

Hardening (taskschd.msc → task properties — per the scheduling playbook):
"Run whether user is logged on or not"; UNTICK AC-power conditions;
"Run task as soon as possible after a scheduled start is missed";
"If already running: Do not start a new instance"; "Stop the task if it
runs longer than" 30 min. Laptop sleep = missed runs — the CLI is
idempotent and gap-tolerant (actual timestamps recorded; `snapshot-stale`
alerts when the pipeline gaps). Query health:
`schtasks /Query /TN dexpaprika\alerts /V /FO LIST`.

### Reports for a fresh Claude session (read order)

1. `dexpaprika healthcheck --json` — is the system trustworthy right now?
2. `dexpaprika report --json` — portfolio by group with as_of.
3. `dexpaprika hedge status --json` — coverage/quadrant/flags.
4. `dexpaprika alerts log --json` — what fired lately and was it delivered.
5. `dexpaprika quota --json` — remaining API headroom.

## System-wide failure modes & recovery (S10)

Triage order when things look wrong: `healthcheck` → the failing check's
recovery above → `alerts log --json` (did the system already tell you?) →
the table below.

| Symptom | Meaning | Recovery |
|---|---|---|
| "RPC ring exhausted" (exit 1) | every peer for that chain failed | transient: retry next tick. Persistent: check network, extend the ring (`DEXPAPRIKA_BASE_RPC_URLS` / `DEXPAPRIKA_ARBITRUM_RPC_URLS`) |
| "circuit open for '<provider>' … retry in Ns" | that upstream failed repeatedly; breaker is cooling down | wait it out (~60s) — the breaker half-opens by itself; do not hammer |
| "credit budget exhausted for '<provider>'" | monthly credit budget spent — waiting will NOT help | stop calling that provider until next month, or raise the seeded budget deliberately; `quota --json` shows usage |
| `snapshot-stale` alert / `last_snapshot_age` fail | recorder gapped (laptop asleep, task disabled) | `schtasks /Query /TN dexpaprika\recorder /V /FO LIST`; then run `dexpaprika snapshot --json` and confirm the alert clears |
| `naked-lp` alert | GMX short GONE (SL fired or closed) while the LP is live | verify on GMX; decide re-arm per the Insurance Policy playbook (S9 execution still requires explicit go-ahead) |
| DB corrupt (integrity fail / "file is not a database") | disk issue or interrupted write | restore drill: `dexpaprika db restore --json` (newest verified backup), then `db status --json` must say integrity ok — drill is tested in the gate suite |
| `PinMismatchError` on chain snapshot | load-balanced RPC served a lagging node | re-run; if persistent, reorder/extend that chain's RPC ring |
| alerts exit 3, `ntfy_status` recorded | delivery failed but the firing IS recorded | fix connectivity/topic, next cooldown-expired check re-delivers; nothing is lost |
| clock skew fail | Windows clock drifted | resync time; until then staleness ages, cooldowns, and quota windows are unreliable |

## Execution (S9 — PRIVILEGED; separate command scope)

```
dexpaprika execute status --json                     # armed? kill switch? limits?
dexpaprika execute set-sl-trigger --price 1926 --json          # DRY-RUN (default)
dexpaprika execute arm [--ttl-minutes 30] --json     # step 1 of going live
dexpaprika execute set-sl-trigger --price 1926 --arm --json    # live (gated)
dexpaprika execute resize-short --target-eth 5.10 --json
dexpaprika execute cancel-order --key 0xORDERKEY --json
```

- **Dry-run is the default everywhere**: without `--arm` the command builds
  and simulates the full plan, records intent+simulation in the audit log,
  and sends NOTHING. Going live requires BOTH `execute arm` (creates the
  expiring armed-state file) AND the `--arm` flag on the order command.
- **Every live order needs Richard's approval**: an urgent ntfy message
  restates the action, sizes, and plan; reply `approve <id>` (or
  `reject <id>`) on the topic within 10 min. Timeout = rejected. A bare
  "yes" can never fire anything — approval binds to the instruction id.
- **Hard limits enforced in code before any network call**: max position
  $20k, max delta per run $5k, max 4 adjustments/day, ETH/USD only, min
  60s between submissions (all env-tunable; audit-counted).
- **Kill switch**: create the file `KILL-SWITCH` in the data dir to halt
  ALL mutating behaviour instantly. The system trips it itself on 3
  consecutive failed submissions or any post-condition mismatch. NO code
  path removes it — deleting the file manually is the only re-arm.
- **Audit trail**: append-only `audit_log` — intent → simulation →
  submission → confirmation, plus blocked/rejected rows with reasons.
  Idempotency: the same decision in the same hour replays the stored
  response; a crash between submit and confirm re-uses the SAME venue-side
  idempotency key, so double-fire is impossible.
- **Key custody**: the executor holds only the GMX subaccount key (secret
  `gmx_subaccount_key`) — scoped on-chain (action count + expiry), can
  trade the GMX account, can NEVER withdraw funds. Until the supervised
  setup session creates and authorizes it, live submission fails closed.
- Sidecar: `executor/gmx_exec.cjs` (official `@gmx-io/sdk`, pinned
  lockfile; `cd executor && npm ci`). Needs Node.js; the sidecar holds no
  policy — every safeguard lives in the Python gate chain.

## Deployment (S11 — container/VPS)

```
docker build -t dexpaprika:latest .
DEXPAPRIKA_SECRET_NTFY_TOPIC=yourtopic docker compose up -d
docker compose run --rm scheduler healthcheck --json   # one-off ops via the same image
docker compose logs -f scheduler                       # one JSON line per job run
dexpaprika scheduler jobs --json                       # inspect the schedule
```

- The image is multi-stage, non-root, pinned base; ENTRYPOINT is the CLI, so
  ANY runbook command works via `docker compose run --rm scheduler <cmd>`.
- The `scheduler` service runs `dexpaprika scheduler run` (scheduling
  playbook Option B — the container/VPS counterpart of schtasks): snapshot
  hourly on the hour, `alerts check` every 5 min
  (`DEXPAPRIKA_SCHEDULER_ALERTS_MINUTES`), `db backup` daily 03:10 UTC —
  every job `max_instances=1`, `coalesce=True`, `misfire_grace_time=120s`,
  so restarts/sleeps produce ONE catch-up run, never a backlog storm.
- Secrets: the provider swap in action — containers use the env backend;
  pass `DEXPAPRIKA_SECRET_*` from the HOST environment (or an orchestrator
  secret store). Values never go in compose.yaml, the image, or git.
- Data lives in the `dexpaprika-data` volume; the daily `db backup` job
  writes verified backups inside it (restore drill: RUNBOOK Database
  section works unchanged in-container).
- Timescale/Postgres path (rehearsed, not just documented): translate +
  apply the packaged migrations to a disposable TimescaleDB and verify
  hypertables with `scripts/pg_rehearsal.py` (see its docstring for the
  exact docker commands; last report: probes/out/s11/pg_rehearsal_report.json).
- Release artifact: `make release` → wheel + sdist + CycloneDX SBOM at
  `dist/sbom.cdx.json` (frozen runtime dependency set).
- VPS note: a persistent host unlocks SSE streaming (e.g. `ntfy subscribe`
  for the S9 approval loop, streaming price feeds) that scheduled one-shot
  runs cannot hold open. The scheduler service is already the persistent
  process such features would attach to.

## Gates (build sessions)

```
make test    # offline gate suite (what the fresh-agent verifier runs)
make audit   # bandit + pip-audit (needs network)
make smoke   # LIVE read-only smoke suite (real upstreams, throwaway data dir)
```

The gate now also enforces doc integrity: every `dexpaprika` command in this
file must parse against the real CLI, and every referenced repo path must
exist — if you edit this RUNBOOK, `make test` checks your work.
