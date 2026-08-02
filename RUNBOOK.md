# RUNBOOK.md — operating dexpaprika

Written so a fresh Claude session can operate the system from this file alone.
Every command supports `--json` (use it — you are the operator). Exit codes:
0 ok, 1 operational failure (JSON `{"error": ...}` explains what to fix),
2 usage error, 3 degraded (healthcheck only).

## First reads in any session

```
dexpaprika status --json        # what exists, wallet counts, config summary
dexpaprika healthcheck --json   # exit 0 only if ALL checks pass; 3 = degraded
```

Healthcheck checks reporting `not-implemented` are pending later sections —
degraded (exit 3) is the EXPECTED state until the full system is built.
A check reporting `fail: ...` tells you exactly what to fix.

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
dexpaprika wallets add --chain evm|btc|solana --address <addr> [--label <name>] --json
dexpaprika wallets remove  --address <addr> | --label <name>   # --json
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
- A GMX `full_close` event appearing = the short is GONE (SL fired or
  closed) — this is the alertable state S8 watches.
- Holdings show amounts only (pricing joins in S7/S8).

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

## Gates (build sessions)

```
make test    # offline gate suite (what the fresh-agent verifier runs)
make audit   # bandit + pip-audit (needs network)
```
