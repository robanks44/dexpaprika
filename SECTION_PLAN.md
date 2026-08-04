# SECTION_PLAN.md — dexpaprika build sections

Ordered, dependency-aware. One section = one LOOP_PROMPT.md iteration through all
gates (reference gate → probe gate → spec → tests-first → implement → fresh-agent
verify → merge + tag). Reference docs resolve via `loop/REFERENCE_INDEX.md`; §0/§0.1
findings are read every section. Coverage gate: ≥90% core logic, ≥80% overall.

Status legend: `pending` → `in_progress` → `complete` (tracked in PROGRESS.md).

---

## S0 — Scaffold & tooling ✅ (this setup session)

- **Goal:** repo skeleton per ENGINEERING_STANDARDS/GIT_RULES: git+hooks, uv project
  pinned Python 3.13, src layout, ruff + mypy(strict) + pytest + coverage +
  hypothesis + pytest-socket, CLI stub (`--json`, `status`, `healthcheck`), Dockerfile
  + compose, `.env.example`, `make test` offline gate, loop files under `loop/`.
- **Interfaces:** `dexpaprika` entrypoint (stubs exit cleanly, emit JSON).
- **References:** python--best-practices--project-structure.md; pytest--best-practices.md.
- **Test focus:** gate suite runs offline and green on an empty system; CLI stubs
  return valid JSON and correct exit codes.
- **Done:** fresh-agent verifies clean env → full gates green; tag `s0-complete`.

## S1 — Config, secrets & wallet registry

- **Goal:** `pydantic-settings` env-first config (data dir, chains, RPC lists,
  thresholds, hard limits); `SecretProvider` (keyring local / env cloud); wallet
  registry table + `dexpaprika wallets list|add|remove|include|exclude` (EVM, BTC,
  Solana address validation); real CLI framework decision.
- **Depends:** S0.
- **References:** python-keyring--setup--windows.md; §6 project-structure.
- **Test focus:** config precedence; secret provider fake; address validation per
  chain family; include/exclude drives downstream selection; no secret ever logged.
- **Done:** wallets CRUD usable via CLI --json; healthcheck reports secrets present.

## S2 — Storage & migrations

- **Goal:** SQLite WAL, FK on, migrations runner (SQL files, `schema_migrations`),
  ARCHITECTURE §3 schema, backup + tested restore, `PRAGMA integrity_check` in
  healthcheck; Decimal-as-TEXT discipline; Postgres/Timescale dialect kept viable.
- **Depends:** S1.
- **References:** sqlite--best-practices.md; timescaledb--api-reference--lp-tracker.md;
  defi-portfolio--best-practices.md §8.
- **Test focus:** migration up/down idempotence; crash-resume (re-run without
  corruption); restore-from-backup round trip; Decimal round-trip property tests.
- **Done:** empty DB → migrated → healthcheck green; backup/restore verified in tests.

## S2.5 — Provider quota tracker

- **Goal:** REFERENCE_INDEX §3b design: `providers` + endpoint-cost config tables,
  universal `api_call_log`, token-bucket rate limiting per upstream (in-process),
  weighted credits (Krystal ~10/call, CoinStats 400/DeFi call) and flat (DexPaprika)
  as first-class; `dexpaprika quota` command. S3/S4/S5 all depend on this —
  retrofitting later means touching every client.
- **Depends:** S2.
- **References:** §3b (self-contained); coinstats--api-reference.md;
  krystal--api-reference--client.md.
- **Test focus:** limit enforcement per upstream (not per instance); credit
  accounting property tests (never exceeds budget for any call sequence); frozen-time
  bucket refill; queryable spend.
- **Done:** a fake client driven through the tracker respects rate + credit budgets;
  quota command reports spend from the log.

## S3 — DexPaprika client

- **Goal:** typed client (networks, dexes, pools, tokens, OHLCV, transactions) behind
  the quota tracker at 30 req/min; source recorded per datapoint; fallback seam to
  GeckoTerminal/CoinGecko for reference prices.
- **Depends:** S2.5.
- **References:** ../dex-docs/; dexpaprika-context-docs/llm.md (grep); live docs
  (base `https://api.dexpaprika.com`; verify batch-prices + filtering at build).
- **Probe:** re-dump pool details + OHLCV for Richard's pool; confirm `fee` still
  null; capture any rate-limit headers.
- **Test focus:** respx-mocked recorded payloads; skew guard (DexPaprika price never
  feeds range logic); 429/5xx retry + circuit breaker; pydantic boundary models.
- **Done:** pool/OHLCV reads recorded to DB with source + as_of; gates green.

## S4 — GMX data client

- **Goal:** typed REST client (positions incl. relatedOrders, orders, markets) with
  peer rotation, scaling layer (1e30 / 1e4 / token decimals / **1e12 triggerPrice**),
  `sizeDeltaUsd == uint256 max` = full close; defensive parsing ("Expanding" API).
- **Depends:** S2.5.
- **References:** APIDOCS/gmx-docs (api + trading mechanics); VERIFIED_FINDINGS §2;
  probe dump `probes/out/gmx_positions.json` (2026-08-02, fixture source).
- **Probe:** live positions call; assert entry ≡ sizeInUsd/sizeInTokens; SL order
  present with trigger 1925.0 after scaling.
- **Test focus:** scaling table is exhaustively property-tested (a bug here = silent
  wrong alerts); empty `[]` handled as valid; peer failover; funding/borrow fee fields.
- **Done:** hedge leg readable end-to-end into DB; gates green.

## S4.5 — EVM on-chain read layer

- **Goal:** web3.py raw-call reader: block-pinned snapshots (pin = head − margin,
  `block_identifier` everywhere, Multicall3 `getBlockNumber` tripwire), mini-ABI
  `eth_call` helpers (slot0 decode, int24 sign-extend), RPC failover matrix from
  config (Base + Arbitrum quirks), Aave v3 account reads.
- **Depends:** S2.5.
- **References:** web3py--api-reference.md; rpc-providers--api-reference--base-arbitrum.md;
  aave-v3--integration-guide.md; probe `probes/out/onchain_base_probe.json`.
- **Test focus:** pin assertion trips on mismatched block; sign-extension property
  tests; failover on 429/403-UA; skew-discard rule for off-chain sources.
- **Done:** one command captures a pinned multi-read snapshot on Base + Arbitrum.

## S5 — LP discovery & valuation

- **Goal:** ARCHITECTURE §5.1 recipe: Sickle lookup (`SickleFactory.sickles(wallet)`,
  `owner()` verification), config-driven NFPM registry (≥2 SlipStream deployments),
  enumeration (`balanceOf`/`tokenOfOwnerByIndex`/`positions`) + CLGauge staked path;
  amounts from ticks (CL math), fee/reward reads; aggregator hints verified on-chain.
- **Depends:** S4.5.
- **References:** aerodrome-slipstream--integration-guide--gauges.md (ONE path, not
  THE answer — §0.1a); concentrated-liquidity-math--summary.md;
  aerodrome--integration-guide.md; krystal docs (hint source; coverage gap §0.1d).
- **Probe:** full recipe against Richard's wallet — expect NFT #5056427 via Sickle
  `0x6c1b2006…` with ticks −202000..−200000; dump payloads as fixtures.
- **Test focus:** discovery finds wallet-held, Sickle-held and gauge-staked positions
  in fixtures; amount-from-tick property tests vs recorded reality (in/below/above
  range regimes); zero-position wallets yield clean empties, not errors.
- **Done:** `dexpaprika snapshot --kind lp` records the live position with correct
  ticks, amounts, pending fees; gates green.

## S5.5 — BTC & Solana wallet clients

- **Goal:** Blockstream Esplora client (balances/UTXOs, address-format handling) and
  Solana RPC client (SOL + token accounts; Orca/Raydium/Kamino positions ONLY if such
  positions exist on Richard's addresses — confirm before scoping); both behind quota
  tracker; wallet registry drives selection.
- **Depends:** S2.5 (+ addresses from Richard — BLOCKER until provided).
- **References:** bitcoin--integration-guide.md; solana--integration-guide--lp-tracker.md;
  solana--summary--quick-reference.md; orca/raydium/kamino guides if in scope.
- **Probe:** first real call per address (probe gate) — cannot run until addresses land.
- **Test focus:** recorded fixtures; UTXO sum property test; commitment-level handling.
- **Done:** included BTC/Solana wallets appear in portfolio snapshots with source+as_of.

## S6 — Portfolio analysis & recording jobs

- **Goal:** normalization to the unified position model; append-only lifecycle events
  (open/modify/partial_close/full_close/harvest) by snapshot diffing; grouping
  (defi = lend/borrow incl. Aave; lp_hedge = LP + GMX short; holdings = the rest);
  `dexpaprika snapshot` orchestration (idempotent, resumable) + `report` command.
- **Depends:** S3, S4, S4.5, S5 (S5.5 folds in when unblocked).
- **References:** defi-portfolio--best-practices.md §2–3 (§4 SDK-aggregation, §9
  distribution, Redis patterns OUT of scope — Richard-confirmed);
  defi-position-aggregation--playbook.md (weakened Option B caveat);
  defi-tax-tracking--best-practices.md.
- **Test focus:** lifecycle event derivation from snapshot pairs (property: events
  replay to end state); double-count guards (Zerion filter-mode lesson); group sums.
- **Done:** scheduled hourly job records full portfolio; report renders both groups
  with per-datapoint source/as_of.

## S7 — Hedge coverage engine (school-material reconciled)

- **Goal:** LP delta curve (9.01 → 3.42 → 0 ETH regimes), coverage ratio, band-edge +
  SL-correlation analysis (top-exit ⇒ delta→0 ⇒ naked short — the verified structural
  failure mode), what-if simulation (`hedge simulate`); alert thresholds; strategy
  options (delta-matched rebalance bands / put ladder / SL ladder + re-entry)
  evaluated ANALYTICALLY (no execution).
- **Depends:** S6.
- **References:** encylopedia Uig 4_Liquidity providers + 4.1–4.9, 5.1–5.6,
  5.action items, 43_criteria.xlsx (**ground truth — reconcile before coding; log
  reconciliation in PROGRESS.md**); concentrated-liquidity-math--summary.md;
  personal\insurance-policy--strategy.md; deribit--api-reference--eth-options.md
  (puts alternative); GMX trading mechanics docs (funding/borrow/liquidation).
- **Test focus:** hypothesis property tests — coverage never negative; delta
  monotonic across tick range; conservation at band edges; sizing never exceeds
  limits; fixture-anchored regression on the live position's numbers.
- **Done:** `hedge status`/`simulate` produce school-material-consistent analysis on
  recorded fixtures; verifier green.

## S8 — Reporting & alerts for Claude

- **Goal:** ntfy client (priorities, tags, action buttons); alert rules engine over
  hedge_state + snapshots (band-edge distance, coverage drift, SL proximity,
  staleness, quota exhaustion, healthcheck degradation); `alerts_log` with firing
  inputs; report formats consumable by a fresh Claude session (RUNBOOK-documented).
- **Depends:** S7.
- **References:** ntfy--api-reference.md; python-scheduling--playbook--windows.md
  (schtasks definitions: hourly recorder, minutes-scale monitor, catch-up,
  no-overlap, hang guard).
- **Test focus:** rule firing on synthetic states; delivery failure → recorded, not
  lost; no secret (topic) in logs; scheduler definitions validated.
- **Done:** end-to-end: scheduled monitor detects a synthetic threshold breach and a
  real ntfy message lands on Richard's topic (one live smoke, excluded from gate).

## S9 — Hedge order execution (privileged) — BUILT + live-exercised 2026-08-04 (on-chain)

> **UPDATED 2026-08-04:** built and exercised live. Write path is **on-chain GmxSdk
> (Classic)**, not express/subaccount (GMX frontend-only — see PROGRESS decision log).
> Sidecar `executor/gmx_exec_onchain.cjs`; `gmx_wallet_key` secret; move-SL =
> create-new-then-cancel-old. **OWED:** tests-first + fresh-agent verification of the
> on-chain sidecar write path (see S9.6 below); the 40 Python safeguard tests still pass.

- **Goal:** ARCHITECTURE §7 in full: `execute` command (separate scope), dry-run
  default, `--arm` + armed-state file, non-agent-overridable kill switch, hard limits
  pre-client, idempotency keys, audit-before-attempt, ntfy approval flow (action
  buttons + polling), GMX write path — **realized as an on-chain Node sidecar over the
  official `@gmx-io/sdk` `GmxSdk`** (cancel-and-recreate for SL edits).
- **Depends:** S7, S8 + **Richard's go-ahead recorded in PROGRESS.md**.
- **References:** gmx-python-sdk--api-reference.md; APIDOCS gmx api/contracts;
  ntfy--api-reference.md; OWASP Agentic Top 10 2026 (read official doc at build).
- **Test focus:** every safeguard has a test that proves it BLOCKS (limits, kill
  switch, unapproved, replay, double-fire on crash-restart); simulation/post-condition
  verification; audit completeness property (no action without prior intent record).
- **Done:** dry-run e2e green; live path exercised only with Richard supervising;
  OWASP review logged; verifier green.

## S10 — Whole-system integration & runbook

- **Goal:** full RUNBOOK.md (every command, failure modes, recovery — operable by a
  fresh Claude session alone); healthcheck completeness (ENGINEERING_STANDARDS §2
  list); cross-section integration tests; live read-only smoke suite (marked, out of
  gate); reference link-integrity check wired into `make test`.
- **Depends:** S1–S8 (S9 if built).
- **Test focus:** fresh-agent full-suite + healthcheck + smoke; simulated failure
  drills (dead RPC, stale snapshot, quota exhausted, DB restore).
- **Done:** whole-system check per LOOP_PROMPT Step 8.

## S11 — Cloud packaging

- **Goal:** container parity verified (compose runs scheduler-driven CLI), Timescale
  migration executed against a disposable Postgres, secret-provider swap exercised,
  CycloneDX SBOM on release artifact, deployment notes (VPS unlocks SSE streaming).
- **Depends:** S10.
- **References:** timescaledb docs; ENGINEERING_STANDARDS §3 (SBOM), §6.
- **Done:** same artifact green locally and in container; migration rehearsed;
  RUNBOOK updated.

## New sections (added 2026-08-04 from the founding decision log; not yet built)

### S9.6 — On-chain executor verification (OWED)
- **Goal:** tests-first + fresh-agent verification of `executor/gmx_exec_onchain.cjs`
  (read/prepare/submit; set-sl-trigger create-new→cancel-old; cancel-order): scale/clone
  correctness, nonce-safe sequencing, wallet-address == account guard, dry-run parity.
- **Depends:** S9. **Done:** fresh-agent green; tag.

### S12 — Recorder service + LIVE dashboard
- **Goal (see 2026-08-04 decision):** persistent local recorder service — DexPaprika SSE
  (LP ~1s) + GMX REST poll (hedge) → SQLite (WAL) → dashboard served locally + SSE push
  (browser never polls upstream). Records the FULL raw variable set (S6 must capture it);
  derived-metrics section computed at query/display time. Honest per-source staleness.
  Static HTML export demoted to a secondary CLI command.
- **Depends:** S5 (range bounds for derived metrics), S6 (full-variable recording).
- **References:** flask--best-practices--production.md; python-scheduling--playbook--windows.md
  (service-at-logon / NSSM). **Standards amendment DONE (2026-08-04):** ENGINEERING_STANDARDS
  §6 — daemon required for LIVENESS; correctness stays CLI + external-scheduler achievable.

### S13 — External watchdog + daily digest
- **Goal:** heartbeat to an EXTERNAL dead-man's-switch (e.g. healthchecks.io free tier —
  NOT on the watched machine); silence self-alerts; daily "all is well" position digest to
  ntfy (replaces the once-daily manual check). **Depends:** S8, S12.

### S14 — Delta-band rebalance hedge strategy
- **Goal:** replace the SL ladder with delta-matched rebalancing — resize the GMX short to
  track live LP ETH exposure; rebalance on delta drift, not price. Uses S9 execution.
- **Depends:** S5 (live tickLower/tickUpper — the §0.1(a) custody blocker), S7, S9.
- **Interim until then:** keep SL, widened toward the 2%-of-capital cap (2026-08-03 decision).

---

**Open items feeding this plan** (tracked in PROGRESS.md): BTC + Solana addresses
(blocks S5.5); GitHub PAT (remote push, any section); S9 build go-ahead (gate on S9);
S5 range-bounds custody blocker **RESOLVED 2026-08-04** (probe-verified live; S12
metrics + S14 unblocked).
