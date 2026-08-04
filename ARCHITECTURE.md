# ARCHITECTURE.md — dexpaprika

DeFi investor system for (1) portfolio analysis, (2) data recording, and (3) active
management of a GMX short hedge protecting LP downside risk. Claude is the operator;
every capability is CLI-first with `--json` output (ENGINEERING_STANDARDS §0).

Scope confirmed by Richard 2026-08-02 (setup Step 1):

- LP: Aerodrome SlipStream (2nd deployment) WETH/USDC on Base, pool
  `0x56aeaf4af2df4bdfd9d865830fefdd278b25e7ef`; custodied by Richard's vfat.io
  **Sickle** smart wallet `0x6c1b20062970c886082687d8121d06aaace8886e` (probe-resolved,
  see §5.1).
- Hedge: ETH/USD short on GMX v2 Arbitrum (market `0x70d95587…`), SL trigger $1,925
  (orderType 6, full close, autoCancel).
- Portfolio recording: **full portfolio** — EVM wallet `0xC155A616…d7Fe` across chains,
  plus BTC and Solana wallets (addresses TBD), with a wallet registry supporting
  add/remove and include/exclude. DeFi grouping: lend/borrow (Aave) grouped separately;
  LP grouped with the hedge. Aave is OUT of hedge scope (recorded only).
- Cadence: LP recorder hourly; hedge monitor minutes-scale. Alerts via ntfy topic
  (secret-stored), alert-only to start.
- S9 programmatic order execution: approved for the section plan; **building it still
  requires Richard's explicit go-ahead at that section** (§7).
- Git: private GitHub remote approved; PAT pending.

## 1. Component map

```
                          ┌─────────────────────────────────────────┐
                          │        CLI (single entrypoint)          │
                          │  dexpaprika <cmd> --json                │
                          │  status | healthcheck | wallets |       │
                          │  snapshot | report | hedge | quota |    │
                          │  execute (S9, separate command+scope)   │
                          └───────┬───────────────────┬─────────────┘
                                  │                   │
              ┌───────────────────▼─────┐   ┌─────────▼──────────────┐
              │     Portfolio engine    │   │     Hedge engine       │
              │ normalize + lifecycle   │   │ CL math, delta curve,  │
              │ events, grouping        │   │ coverage ratio, band   │
              │ (defi / lp+hedge /      │   │ logic — reconciled w/  │
              │  holdings)              │   │ encylopedia Uig        │
              └───────────┬─────────────┘   └─────────┬──────────────┘
                          │                           │
        ┌─────────────────▼───────────────────────────▼───────────┐
        │                    Storage (SQLite WAL)                  │
        │ wallets · providers · api_call_log · snapshots ·         │
        │ positions · position_events · hedge_state · orders ·     │
        │ alerts_log · audit_log · schema_migrations               │
        └─────────────────┬────────────────────────────────────────┘
                          │
        ┌─────────────────▼────────────────────────────────────────┐
        │              Quota tracker (REFERENCE_INDEX §3b)         │
        │  per-provider config: rate limit, credit weights, tiers  │
        └───┬───────┬────────┬────────┬────────┬────────┬──────────┘
            │       │        │        │        │        │
        ┌───▼──┐ ┌──▼───┐ ┌──▼────┐ ┌─▼────┐ ┌─▼────┐ ┌─▼─────────┐
        │DexPap│ │GMX   │ │EVM RPC│ │BTC   │ │Solana│ │Aux: Gecko-│
        │rika  │ │REST  │ │reader │ │Esplo-│ │RPC   │ │Terminal,  │
        │client│ │client│ │(web3, │ │ra    │ │      │ │CoinGecko; │
        │      │ │      │ │block- │ │      │ │      │ │key-gated: │
        │      │ │      │ │pinned)│ │      │ │      │ │Krystal etc│
        └──────┘ └──────┘ └───────┘ └──────┘ └──────┘ └───────────┘

  Alerts: ntfy client (publish + action buttons; approval polling for S9)
  Scheduler: EXTERNAL (Windows Task Scheduler now, cron/cloud later) → calls CLI
  Executor (S9): privileged; own command + scope; dry-run default; kill switch
```

## 2. Data flow

**Hourly LP/portfolio snapshot** (`dexpaprika snapshot --json`, scheduled externally):

1. Resolve block pin per EVM chain (`eth_blockNumber` − reorg margin); every read in
   the snapshot uses `block_identifier=pin`; Multicall3 `getBlockNumber()` asserted
   equal to the pin (tripwire against load-balanced lagging nodes).
2. Discover/refresh positions for each **included** wallet (§5.1 recipe for LP;
   Aave account data; token balances; BTC UTXOs; Solana accounts).
3. Value positions: on-chain state first (pool `slot0` tick is the price source for
   range logic — DexPaprika is history/volume only, ~2% skew verified); reference
   prices from DexPaprika/GeckoTerminal/CoinGecko with `source` recorded per datapoint.
4. Emit position lifecycle events (open/modify/partial_close/full_close/harvest) by
   diffing against last snapshot; append, never mutate.
5. Persist snapshot rows carrying `block_number` (or timestamp for off-chain sources),
   `as_of`, `source`. Off-chain reads skewed >a few seconds from the chain pin are
   DISCARDED, not stored (ENGINEERING_STANDARDS §2).

**Minutes-scale hedge monitor** (`dexpaprika hedge status --json`):

1. GMX REST `/positions?address=…&includeRelatedOrders=true` (peers rotate:
   `arbitrum.gmxapi.io` ⇄ `arbitrum.gmxapi.ai`); scalings per VERIFIED_FINDINGS §2.1
   (1e30 USD fields, 1e4 leverage bps, **1e12 order triggerPrice for ETH**).
2. Pool `slot0` on Base → current tick/price; position ticks → LP delta via CL math
   (`reference\concentrated-liquidity-math--summary.md`).
3. Coverage ratio = short size in ETH ÷ LP ETH delta; band-edge distance; SL distance;
   funding/borrow fee drift.
4. Alert rules (thresholds in config) → ntfy publish with priority/tags; every alert
   recorded in `alerts_log` with the inputs that fired it.

## 3. Storage schema (draft — S2 refines)

SQLite, WAL mode, foreign keys ON, versioned migrations (plain SQL files applied by a
migrations runner; `schema_migrations` table). All money/qty columns TEXT holding
Decimal strings (or INTEGER base units) — never REAL.

- `wallets(id, chain_family {evm,btc,solana}, address, label, included BOOL,
  added_at)` — the registry behind `dexpaprika wallets` (add/remove/include/exclude).
- `providers(id, name, base_url, rate_limit, rate_period, has_credits, credit_limit,
  free_tier, config_json)` + `provider_endpoint_costs(provider_id, endpoint_pattern,
  credits)` — quota config as data (adding a provider is config, not code).
- `api_call_log(id, ts, provider_id, endpoint, credits, status, latency_ms,
  correlation_id)` — universal call log; quota consumption is queryable.
- `snapshots(id, ts, chain, block_number, kind, correlation_id)` — one row per
  chain-pinned capture.
- `positions(id, wallet_id, venue, chain, kind {lp,perp,lend,borrow,holding,order},
  external_id, group_tag {lp_hedge,defi,holdings}, opened_at, closed_at,
  metadata_json)` — current-state index over the event stream.
- `position_events(id, position_id, snapshot_id, ts, type {open,modify,partial_close,
  full_close,harvest,rebalance,observed}, delta_json, state_json, tx_hash)` —
  append-only lifecycle (defi-portfolio best-practices §3 pattern, adopted).
- `hedge_state(id, snapshot_id, ts, lp_delta_eth, short_size_eth, coverage_ratio,
  pool_tick, band_lower_tick, band_upper_tick, sl_trigger, distances_json)`.
- `orders(id, ts, venue, external_key, order_type, trigger_price, size_delta,
  status, raw_json)` — observed venue orders (SL etc.).
- `alerts_log(id, ts, rule, severity, payload_json, delivered BOOL, ntfy_status)`.
- `audit_log(id, ts, actor, action, phase {intent,simulation,submission,confirmation,
  blocked,rejected}, idempotency_key, payload_json)` — append-only; written BEFORE
  privileged attempts (§7).

Postgres/Timescale migration path (§8): identical DDL semantics; time-series tables
(`snapshots`, `position_events`, `hedge_state`, `api_call_log`) become hypertables;
Decimal-as-TEXT becomes NUMERIC. Migrations runner targets both dialects from S2.

## 4. Config & secrets

- `pydantic-settings`, env-first, `.env` for local dev only (gitignored;
  `.env.example` documents every variable). No machine-specific paths in code — data
  directory comes from config; the system reads/writes only inside it.
- Secrets: OS keyring (Windows Credential Manager) locally via `keyring`;
  `SecretProvider` seam so cloud swaps to env/secret-manager without code changes.
  Planned migration: keyring → sops+age (if secrets must travel) → cloud secret
  manager with workload identity (ENGINEERING_STANDARDS §3). Named secrets to date:
  `github_pat` (repo push), `ntfy_topic` (treated as a secret — knowing it allows
  spoofed alerts), optional provider keys (DexPaprika free key, Krystal, CoinStats…).
- Hard limits for S9 (max position size, max delta per run, max daily adjustments,
  allowed markets) live in config, enforced in code before the client layer.

## 5. Clients (all behind the quota tracker; pydantic models at every boundary)

Per-client: explicit timeouts, tenacity retry w/ backoff+jitter, circuit breaker per
upstream, HTTPS enforced, 10 MB response cap, ~0.6 s inter-request delay defaults.

- **DexPaprika** — market data/history/volume. Public, no key; enforce 30 req/min
  client-side. NOT the price source for range logic (verified ~2% skew). `fee` is
  null for SlipStream pools — fee tier comes on-chain. New since mirror: SSE
  streaming (unusable under external scheduler; VPS-only), pool filtering, batch
  token prices — S3 verifies live.
- **GMX REST** — positions/orders/markets, no key. Peers rotate; empty `[]` is a
  valid "no positions" (HTTP 200). Scaling table is a hard test target: a scaling
  bug = silent wrong alerts. Status "Expanding" → parse defensively.
- **EVM RPC reader** — web3.py raw `eth_call` patterns, block-pinned snapshots,
  Multicall3 batching, provider failover (`mainnet.base.org` 429s fast;
  `arb1.arbitrum.io/rpc` requires a User-Agent header). RPC endpoint list is config.
- **BTC (Blockstream Esplora)** and **Solana RPC** — balance/UTXO and account reads
  for the wallet registry (addresses TBD from Richard; probe gate applies at build).
- **Aux**: GeckoTerminal (documented OHLCV source), CoinGecko (reference prices).
  Key-gated, discovery-only: Krystal (coverage gap verified — missed the live 0xa990
  position), Zerion (`sync=true` mandatory; double-count trap), CoinStats (400
  credits/DeFi call — scope to one chain). Aggregators are HINTS; on-chain verifies.

### 5.1 LP discovery (custody blocker RESOLVED — probed 2026-08-02)

"The wallet does not hold it" is not evidence a position does not exist — verify
custody, not ownership. Probe-verified recipe, generalized:

1. Candidate owners = `{wallet}` ∪ `{SickleFactory.sickles(wallet)}` (vfat.io Sickle
   smart wallet; Base factory `0x71D234A3e1dfC161cc1d081E6496e76627baAc31`,
   implementation `0xFfF75D099baeE29F447866bC5299Cd67C04761C8`; `owner()` on the
   Sickle returns the wallet — verified live: Richard's Sickle is `0x6c1b2006…`,
   and `NFPM.ownerOf(5056427)` = that Sickle).
2. NFPM **registry, not a hardcoded address** — ≥2 SlipStream deployments on Base:
   canonical `0x8279…` and `0xa990…` (holds Richard's position). Config-driven list.
3. For each (owner, NFPM): `balanceOf` → `tokenOfOwnerByIndex` → `positions(tokenId)`
   → ticks, liquidity. Plus the CLGauge staked path
   (`Voter.gauges(pool) → stakedValues(depositor)`) for gauge-staked positions.
4. Aggregator hints (Krystal/Zerion/CoinStats) may ADD candidates; every candidate is
   verified on-chain before it becomes a position.

## 6. CLI design (agent-first, ENGINEERING_STANDARDS §0)

Few, coarse commands; consolidated reads; errors say what to fix. All commands accept
`--json`; exit codes: 0 ok, 1 operational failure, 2 usage error, 3 degraded
(healthcheck). `simulate`/`status` vs `execute` are separate commands with separate
scopes — never flags on one command.

```
dexpaprika status            # one-call operational overview (agent's first read)
dexpaprika healthcheck       # DB integrity, migrations, upstreams, secrets, clock,
                             # last-snapshot age, repo state, dry-run/armed, kill switch
dexpaprika wallets list|add|remove|include|exclude
dexpaprika snapshot [--kind lp|portfolio|all]
dexpaprika report [--period ...]      # portfolio + hedge analysis for Claude
dexpaprika hedge status|simulate      # read-only coverage + what-if
dexpaprika quota [--provider ...]     # spend vs budget from api_call_log
dexpaprika execute ...                # S9 ONLY, separate command; dry-run default
```

## 7. Privileged-action safety (S9 — BUILT + live-exercised 2026-08-04, on-chain)

> **UPDATED 2026-08-04 (see PROGRESS.md decision log for full ADRs).**
> **Execution mechanism = on-chain GmxSdk ("Classic"), NOT express/subaccount relay.**
> GMX exposes express/gasless/One-Click subaccount orders to its frontend only (confirmed
> in GMX's agent docs) — that path is a dead end for automation (relay-router migration,
> stranded subaccount, fee-token rejection). The executor now signs each tx with the
> account wallet (`gmx_wallet_key`, submit-only) and pays ETH gas + keeper fee. Orders are
> **cancel-and-recreate** (GMX can't modify an order): move-SL = create a new
> StopLossDecrease at the new trigger, wait for it to mine (nonce), then cancel the old.
> Sidecar `executor/gmx_exec_onchain.cjs`; endpoints RPC `arb1.arbitrum.io/rpc`, oracle
> `arbitrum-api.gmxinfra.io`, subsquid `gmx.squids.live/…arbitrum`. Live-verified: SL
> $1,900→$1,901 through arm → phone approval → submit → post-condition verify.
> **Owed:** tests-first + fresh-agent verification of the on-chain sidecar path
> (the 40 Python safeguard tests still hold; the Node write path is not yet TDD-covered).
> Also newly required (2026-08-04 decisions): a persistent recorder service
> + LIVE dashboard (recorder service BUILT in S12a; dashboard is S12b), an EXTERNAL
> dead-man's-switch heartbeat + daily ntfy digest (S13), and the delta-band rebalance
> hedge strategy (S14; replaces the SL ladder now S5 range-bounds have landed).

### 6.1 Recorder service (S12a — BUILT 2026-08-04)

`dexpaprika.recorder`: `run_cycle` is the one-shot recording cycle extracted from
`snapshot` (identical DB effects), and `RecorderService` loops it per-source cadence
for liveness. Correctness never requires the daemon (ENGINEERING_STANDARDS §6): a
series of scheduled `recorder cycle` calls produces the same rows as `recorder run`,
property-tested. Per-source isolation (one failed source backs off, never stops the
loop) and honest staleness (a failed source keeps its stale stamp, flagged not-ok)
are the service's, not `snapshot`'s — `snapshot` keeps its fail-hard contract.
Full-variable capture (RAW only; derived metrics are S12b's read-time concern): LP
state adds both token USD prices + pool 24h volume (DexPaprika, null-with-reason when
absent); hedge state adds the SL order size beside its trigger. Liveness table
`recorder_heartbeat` (migration 0003) is append-only; readers never block the writer.

Per ENGINEERING_STANDARDS §4, designed now so earlier sections leave the right seams
(all safeguards below remain in force; only the venue write-path changed to on-chain):

- `--dry-run` default; live requires `--arm` AND an armed-state file created in a
  separate step. Kill-switch file **outside the agent-writable data dir** (Richard's
  home dir), checked before every privileged action; trips on drawdown/anomalies
  (N failed orders, unexpected balance delta); manual re-arm only.
- Hard limits enforced in code pre-client (max size, max delta/run, max daily
  adjustments, allowed markets) + an order-submission rate limit independent of the
  venue's.
- Idempotency keys derived from decision identity (strategy + signal + time bucket);
  first response stored and replayed on retry; bounded expiry (Stripe model).
- Audit record written BEFORE the attempt; blocked/rejected attempts recorded with
  reasons; append-only intent → simulation → submission → confirmation.
- Approval gate: ntfy action buttons + poll-based approval; confirmation is
  substantive (agent restates reasoning + data), never a bare "yes".
- OWASP Agentic Top 10 (2026) review is part of S9's done-criteria.
- Write path (REALIZED 2026-08-04): a pinned Node sidecar over the official TypeScript
  `@gmx-io/sdk` `GmxSdk` (on-chain Classic mode) — Python owns every safeguard and shells
  out to the dumb sidecar for prepare/submit only. (Original plan was a typed REST/on-chain
  Python client; the Node sidecar reuses GMX's own SDK for order construction + signing.)

## 8. Cloud migration path

12-factor throughout: env-first config, no machine paths, one artifact everywhere.
Local now: Windows, Task Scheduler → CLI; SQLite WAL. Cloud later: container
(multi-stage Dockerfile, non-root, pinned base), cron/cloud scheduler → same CLI;
SQLite → Postgres/Timescale via the S2 migrations runner; keyring → secret manager
via the `SecretProvider` seam; ntfy unchanged. A VPS deployment unlocks DexPaprika
SSE streaming (out of scope until then; poll REST meanwhile).

## 9. Decision log (ADR-lite; full log in PROGRESS.md)

- 2026-08-02 — detect-secrets over gitleaks for pre-commit secret scan (pip-installable;
  GIT_RULES permits either).
- 2026-08-02 — CLI stub on stdlib argparse in S0; CLI framework choice deferred to S1
  when the command surface is designed for real (avoids a premature runtime dep).
- 2026-08-02 — `make test` = offline gate suite (zero network, per §5); `make audit` =
  network-requiring supply-chain checks (pip-audit/osv-scanner DBs); `make gate` = both.
  Reconciles ENGINEERING_STANDARDS §3 (audits every change) with §5 (offline gates).
- 2026-08-02 — probe dumps keep real PUBLIC on-chain addresses (wallet, pools, NFT ids):
  fixtures must match real payloads (§5) and the addresses already appear in committed
  loop docs; GIT_RULES §4 scrubbing applied to secrets/keys/tokens, which never land in
  fixtures. Flagged to Richard in the setup handoff.
