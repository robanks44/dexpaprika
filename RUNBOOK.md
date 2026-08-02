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

## Gates (build sessions)

```
make test    # offline gate suite (what the fresh-agent verifier runs)
make audit   # bandit + pip-audit (needs network)
```
