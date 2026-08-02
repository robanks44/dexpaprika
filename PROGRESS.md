# PROGRESS.md — Loop State (single source of truth)

> Keep this file accurate after every step. Assume the next loop iteration runs in a
> fresh session with no memory. Never mark a section complete unless the fresh-agent
> verifier reported all gates passing.

## Status

- Phase: `building`  <!-- not-started | setup | building | complete | blocked -->
- Current section: S7 complete — next: S8 reporting & alerts (ntfy)
- Last updated: 2026-08-02
- Blockers: none (open items below are not blockers for S1–S5)

## Git state (mirror of GIT_RULES.md expectations)

- Last completed tag: s7-complete | main tip at last update: the s7 merge commit (see `git log`)
- Remote configured: no — Richard approved a private GitHub remote (setup Step 1e);
  waiting on his fine-grained PAT. Configure remote + push immediately when it lands.

## Setup phase checklist (from SETUP_PROMPT.txt)

- [x] Loop files + reference folders accessible in session (CONTEXT library connected 2026-08-02)
- [x] Git initialized per GIT_RULES.md §1 (.gitignore-first, hooks, bot identity)
- [x] Remote decision made with Richard (Step 1e): YES, private GitHub; PAT pending
- [x] Scope confirmed with Richard (2026-08-02, see below)
- [x] Research done — findings logged below
- [x] ARCHITECTURE.md written
- [x] SECTION_PLAN.md written
- [x] Scaffold complete; suite + static gates verified clean by fresh agent (2026-08-02)
- [x] Richard approved handoff report (2026-08-02: "begin")

## Scope confirmations (Richard, 2026-08-02)

- Defaults from VERIFIED_FINDINGS.md confirmed in full (LP Aerodrome SlipStream-2
  WETH/USDC Base; GMX v2 ETH short Arbitrum SL $1,925; wallet 0xC155A616…d7Fe;
  hourly LP / minutes-scale hedge cadence; ntfy alert-only start).
- Portfolio recording: FULL portfolio — EVM wallet + BTC + Solana wallets, with a
  wallet registry (add/remove, include/exclude). Grouping: defi (Aave lend/borrow),
  lp_hedge (LP + short), holdings.
- S9 execution: INCLUDED in plan; building it still needs Richard's explicit go.
- Git remote: private GitHub approved; PAT to come.

## Open items (need Richard; none block S1–S5)

1. **BTC + Solana wallet addresses** — blocks S5.5 only.
2. **GitHub fine-grained PAT** (single-repo scope) — store in OS keyring, then
   configure remote + auto-push per GIT_RULES §1.6.
3. **S9 build go-ahead** — gate on S9 (already approved for planning).

## Section log

### S0 — Scaffold & tooling — `complete` (setup phase)
- Attempts: 1
- Branch: built directly on `main` (setup phase, pre-loop; GIT_RULES §2 permits
  docs/state on main — scaffold committed before first verifier run, per SETUP Step 4 order)
- Reference docs read: loop/* (all six), VERIFIED_FINDINGS.md, CONTEXT reference/INDEX.md,
  defi-portfolio best practices (scope notes applied), python--best-practices--project-structure.md,
  pytest--best-practices.md (gate patterns)
- Probe evidence (2026-08-02, committed under probes/out/):
  - `gmx_positions.json` — live GMX REST; scalings re-verified (entry ≡ sizeInUsd/sizeInTokens
    = $1,869.09; SL orderType 6, triggerPrice/1e12 = 1925.0, autoCancel true)
  - `dexpaprika_pool.json` — pool endpoint live; dex_id aerodrome_slipstream_2; fee STILL null
  - `onchain_base_probe.json` — block-pinned reads: pool tick −201142 → $1,840.56/WETH;
    NFT #5056427 ticks −202000..−200000, liquidity 3987414535131380 (in range, live)
  - `sickle_factory_probe.json` — **LP custody RESOLVED**: custodian 0x6c1b2006… is an
    EIP-1167 clone of vfat.io Sickle impl 0xFfF75D09…; `owner()` = Richard's wallet;
    `SickleFactory.sickles(wallet)` (factory 0x71D234A3…) returns it deterministically
- Design notes / decisions: see ARCHITECTURE.md §9 + decision log below
- Test summary: 7 gate tests (CLI JSON contract, exit codes, degraded healthcheck)
- Verifier verdict: "VERDICT: PASS" — fresh agent, clean clone of commit 8e76c91, uv sync --frozen, make test PASS (ruff clean; mypy clean; 7 passed; "Required test coverage of 80% reached. Total coverage: 95.74%"), make audit PASS
- Coverage: 95.74% | ruff: clean | mypy --strict: clean | bandit: clean | pip-audit: clean
  (self-package skip only)
- Test-change justifications: none
- Completed: 2026-08-02

## Research findings log

- 2026-08-02 | LP custody | Custodian = Richard's vfat.io Sickle smart wallet (EIP-1167
  clone, impl 0xFfF75D099baeE29F447866bC5299Cd67C04761C8 = documented Sickle impl on Base;
  factory 0x71D234A3e1dfC161cc1d081E6496e76627baAc31). Discovery recipe in ARCHITECTURE §5.1.
  | live probe + docs.vfat.io/sickle
- 2026-08-02 | GMX REST | Live re-verification: schema + scaling table exactly as
  VERIFIED_FINDINGS §2.1; position and SL unchanged. docs.gmx.io api pages returned 404
  via sandbox fetcher — local mirror + live probe stand as reference. | probe
- 2026-08-02 | DexPaprika | Live docs: base URL + no-key confirmed. NEW since mirror:
  SSE streaming, advanced pool filtering, batch token prices. Free-tier numbers not
  published on the docs page — keep conservative 30 req/min client limit. SSE unusable
  under external scheduler (VERIFIED_FINDINGS §7) until a VPS exists. | docs.dexpaprika.com
- 2026-08-02 | Best practices | ENGINEERING_STANDARDS v2 (revised 2026-08-01 against
  research + probes) verified current; no divergence found worth flagging. New-scope
  additions (BTC Esplora, Solana RPC) covered by library docs verified 2026-08-01. | CONTEXT library
- 2026-08-02 | School material | encylopedia Uig accessible (19 files, 4.x + 5.x + criteria
  + tools); reconciliation deferred to S7 per REFERENCE_INDEX §2. | device folder listing

## Decision log (ADR-lite)

- 2026-08-02 | detect-secrets over gitleaks in pre-commit | pip/uv-installable, no Go
  toolchain; GIT_RULES §1.4 permits either | gitleaks (binary distribution friction)
- 2026-08-02 | S0 CLI stub on stdlib argparse; framework choice deferred to S1 | avoid
  premature runtime dep in lockfile | typer/click now
- 2026-08-02 | `make test` offline vs `make audit` networked split | reconciles standards
  §5 (zero-network gate) with §3 (audits every change) | single command needing network
- 2026-08-02 | Probe dumps keep real PUBLIC on-chain addresses | fixtures must match real
  payloads (§5); addresses already public + present in committed loop docs; GIT_RULES §4
  scrub read as secrets/keys/private data | scrubbing (would invalidate fixtures)
- 2026-08-02 | Tag case `s0-complete` (lowercase) | GIT_RULES §1.5 says `S0-complete`,
  §2 + LOOP_PROMPT say `s<N>-complete` — standardized on lowercase everywhere | mixed case
- 2026-08-02 | Repo canonical copy lives in the Cowork session + (once PAT lands) GitHub;
  a zip snapshot is delivered to Richard's project folder at setup handoff | .git object
  trees don't transfer cleanly file-by-file over the device bridge | committing .git piecemeal

### S1 — Config, secrets & wallet registry — `complete`
- Attempts: 1
- Branch: `section/s1-config-secrets-wallets` | Merge commit: — | Tag: —
- Reference docs read: python-keyring--setup--windows.md (service naming, env-fallback
  chain, Linux-VM silent-None behavior — all adopted); pytest--best-practices.md
  (conftest config fixture isolating real keyring/env; frozen vectors); REFERENCE_INDEX
  §0/§0.1 re-read; python--best-practices--project-structure.md (read at S0, applies).
  Note: keyring doc's "never use .env" adopted for SECRETS; non-secret config may use
  .env per ARCHITECTURE §4.
- Probe evidence (Step 2b): N/A — S1 reads no external API/on-chain source. Address
  validation pinned by published vectors (EIP-55, BIP-173/350, genesis addr, Solana
  program ids) + Richard's live wallet address. Rationale in docs/specs/S1.
- Design notes / decisions: vendored vector-pinned keccak-256 (no crypto dep for one
  hash); registry persisted as JSON doc in S1, S2 may migrate backend behind same API;
  argparse retained (framework re-decision logged:零 runtime dep still preferable);
  BTC validation mainnet-only, witness v0+v1; secret-backend dispatch table (bandit-clean)
- Test summary (written before code): 89 tests — keccak vectors, EIP-55/BIP-173/350
  vectors, registry lifecycle/corruption, Settings env-first + Decimal, provider chain
  + leak tests, CLI e2e; error-path additions post-implementation (justified commit)
- Verifier verdict: "VERDICT: PASS" — fresh agent, clean clone of 012115d,
  uv sync --frozen, make test PASS (89 passed; "Required test coverage of 80%
  reached. Total coverage: 94.04%"), make audit PASS, CLI sanity runs verified,
  healthcheck exit 3 degraded-by-design confirmed, no secret leakage
- Coverage: 94.04% total; core: validation 93%, registry 94% | ruff: clean |
  mypy --strict: clean | bandit: clean | pip-audit: clean (local-package skip)
- Test-change justifications: see commit 012115d message (S0 healthcheck contract
  updated for S1's two implemented checks; unreachable float assert removed;
  error-path tests added for core-coverage gate)
- Completed: 2026-08-02

### S2 — Storage & migrations — `complete`
- Attempts: 1
- Branch: `section/s2-storage-migrations` | Merge commit: — | Tag: —
- Reference docs read: sqlite--best-practices.md (WAL/PRAGMAs/backup API/money storage/
  indexing — adopted); timescaledb--api-reference--lp-tracker.md (hypertable partition
  column requirement → ts column on every time-series table; portable DDL);
  REFERENCE_INDEX §0/§0.1 re-read
- Probe evidence (Step 2b): N/A — local SQLite only, no external source (spec §Probe)
- Design notes / decisions: forward-only migrations (ADR in spec); wallet registry stays
  JSON (S1 ADR upheld); Decimal-as-TEXT over BLOB (auditable, Postgres NUMERIC-castable)
- Test summary (written before code): 28 tests — PRAGMAs/WAL persistence, Decimal-TEXT
  property round-trip, migrations idempotence + atomic rollback + schema sweeps
  (money-not-REAL, ts columns), backup round-trip/corrupt-refusal/pre-restore/pruning,
  CLI db e2e, healthcheck db checks; +3 error-path tests post-impl (justified commit)
- Verifier verdict: "VERDICT: PASS" — fresh agent, clean clone of cba2471, uv sync
  --frozen, make test PASS (117 passed; "Required test coverage of 80% reached. Total
  coverage: 94.08%"), make audit PASS, functional db lifecycle verified end-to-end
  (fresh migrate → idempotent re-run → verified backup → integrity ok), healthcheck
  db_integrity + migrations_current both ok
- Coverage: 94.08% total; storage core: db 100%, migrations 97%, backup 94% | ruff:
  clean | mypy --strict: clean | bandit: clean | pip-audit: clean (local skip)
- Test-change justifications: commit cba2471 (error-path additions only)
- Completed: 2026-08-02
- Fix during implementation worth remembering: SQL header comments contained ';' —
  the statement splitter now strips `--` comments BEFORE splitting (test caught it)

### S2.5 — Provider quota tracker — `complete`
- Attempts: 1
- Branch: `section/s2-5-quota-tracker` | Merge commit: — | Tag: —
- Reference docs read: REFERENCE_INDEX §3b (design implemented as specified);
  coinstats--api-reference.md (400cr/DeFi call, budget guard, 80% alert);
  krystal--api-reference--client.md (pacing/weights); §0/§0.1 re-read
- Probe evidence (Step 2b): N/A — local accounting only; seed values from
  session-verified docs, re-probed per client at S3+
- Design notes / decisions: sliding window computed from api_call_log (DB-backed ⇒
  per-upstream enforcement across instances, §3b); credit budgets per UTC calendar
  month; longest-fnmatch-pattern wins for endpoint weights; 80% alerting deferred to
  S8 rules (tracker reports pct_used)
- Test summary (written before code): 20 tests incl. hypothesis property (any guarded
  call pattern keeps every window under limit), frozen-clock windows, month reset,
  per-upstream two-instance enforcement, CLI e2e
- Verifier verdict: "VERDICT: PASS" — fresh agent, clean clone of a6a089b, make test
  PASS (137 passed; "Required test coverage of 80% reached. Total coverage: 94.78%"),
  make audit PASS, quota CLI functionally verified (9 seeded providers, hyperliquid
  credits-mode window, unknown-provider error)
- Coverage: 94.78% total; tracker 98% | ruff: clean | mypy --strict: clean | bandit:
  clean | pip-audit: clean (local skip)
- Test-change justifications: none
- Completed: 2026-08-02
- Design addition vs §3b (logged): per-provider window MODE (calls|credits via
  config_json) — Hyperliquid's 1200/min is WEIGHT, not calls; a call-count window
  would under-throttle it, and a credits-window would break CoinStats. Both now
  first-class.

### S3 — DexPaprika client — `complete`
- Attempts: 1
- Branch: `section/s3-dexpaprika-client` | Merge commit: — | Tag: —
- Reference docs read: dex-docs/QUICK-REFERENCE.md (endpoint shapes, intervals, limits —
  matched live probe); REFERENCE_INDEX §3 + §0 skew rule re-read
- Probe evidence (Step 2b): probes/out/s3/{networks,pool_details,ohlcv_24h,transactions}
  .json — all 200 live 2026-08-02; fee STILL null for SlipStream pool; OHLCV shape
  time_open/.../volume with JSON floats → transport parses parse_float=Decimal;
  pool 24h window keys confirmed
- Design notes / decisions: shared HttpTransport (quota+retry+breaker+Decimal parse)
  reused by S4+; DexPaprika price NEVER exposed as hedge-math price (role boundary);
  ohlcv upsert idempotent; 0002_market_data migration
- Test summary (written before code): 24 tests from probe fixtures — transport
  (quota/Decimal/retry/breaker/caps), client parsing + recording idempotence,
  role-boundary, CLI e2e; +1 connection-error coverage test post-impl (justified)
- Verifier verdict: "VERDICT: PASS" — fresh agent, clean clone of ba00b3b, make test
  PASS (161 passed; "Required test coverage of 80% reached. Total coverage: 95.34%"),
  make audit PASS; optional live smoke SUCCEEDED (real pool call recorded,
  price Decimal string end-to-end, quota logged the call: window_used=1)
- Coverage: 95.34% total; transport 100%, client 100% | ruff/mypy/bandit/pip-audit clean
- Test-change justifications: commit ba00b3b (coverage addition only)
- Completed: 2026-08-02

### S4 — GMX data client — `complete`
- Attempts: 1
- Branch: `section/s4-gmx-client` | Merge commit: — | Tag: —
- Reference docs read: VERIFIED_FINDINGS §2/§2.1/§2.2; REFERENCE_INDEX §1; dex-docs
  QUICK-REFERENCE GMX section
- Probe evidence (Step 2b): probes/out/s4/ — both peers live+identical; all scalings
  re-verified exactly (trigger/1e12=1925.0, sizeDeltaUsd=uint256max full close);
  PROBE CATCH: /markets has NO indexName — filter positions by indexName, resolve
  index decimals via marketAddress→indexTokenAddress→registry
- Design notes / decisions: numerics parsed as Decimal from JSON strings; unknown
  orderType parsed defensively as unknown-<n>; peer failover at client layer over
  per-peer transports
- Test summary (written before code): 23 tests — exact-Decimal scaling table pinned
  to live numbers + hypothesis precision round-trips, parsing/failover/recording, CLI
- Verifier verdict: "VERDICT: PASS" — fresh agent, clean clone of 479466f, make test
  PASS (184 passed; "Required test coverage of 80% reached. Total coverage: 94.92%"),
  make audit PASS; live smoke PASS: real position recorded, SL trigger Decimal-equal
  1925, is_full_close true
- Coverage: 94.92% total; gmx client 93% | ruff/mypy/bandit/pip-audit clean
- Test-change justifications: commit 479466f — original expectations computed WITH
  the context-rounding bug the impl fixes; checks made stricter, none weakened
- Completed: 2026-08-02
- IMPLEMENTATION CATCH worth remembering: Decimal '/' AND scaleb() round at context
  precision (28 digits) — uint256 raws carry ~78 digits and were silently losing
  tail digits. Fixed with a context-free tuple exponent shift; the exactness tests
  caught it before it shipped. Any future scaler must use the same pattern.

### S4.5 — EVM on-chain read layer — `complete`
- Attempts: 1
- Branch: `section/s4-5-evm-reader` | Merge commit: — | Tag: —
- Reference docs read: web3py--api-reference.md (patterns adopted; dependency NOT — ADR
  in spec); rpc-providers--api-reference--base-arbitrum.md (ring/UA/multicall adopted);
  VERIFIED_FINDINGS §4; §0/§0.1 re-read
- Probe evidence (Step 2b): probes/out/s45/pinned_multicall.json — hand-built aggregate
  calldata live on both chains. STANDARDS CORRECTION FOUND BY PROBE: Multicall3
  getBlockNumber returns the L1 block on Arbitrum (block.number is L1-approximated);
  the §2 tripwire would always fail there. Per-chain tripwire verified instead:
  Base=Multicall3.getBlockNumber, Arbitrum=ArbSys.arbBlockNumber (0xa3b1b31d) — exact
  pin match live. FLAG FOR RICHARD (standards deviation, ENGINEERING_STANDARDS §2).
- Design notes / decisions: raw JSON-RPC + vendored ABI over web3py dep (spec ADR);
  base-rpc/arbitrum-rpc quota providers added; ring order from reference matrix
- Test summary (written before code): 19 tests — encoding pinned byte-for-byte to
  live calldata, tripwire/lag/chainId paths, Arbitrum-ArbSys proof test, failover,
  CLI e2e; +3 rpc error-path tests post-impl (rpc.py -> 100%)
- Verifier verdict: "VERDICT: PASS" — fresh agent, clean clone of 1425857, make test
  PASS (203 passed; "Required test coverage of 80% reached. Total coverage: 95.26%"),
  make audit PASS; live smoke PASS on BOTH chains: base 49424223 tripwire ok,
  arbitrum 490183762 (L2 block — ArbSys tripwire proven in use) tripwire ok
- Coverage: 95.26% total; abi 98%, rpc 100% | ruff/mypy/bandit/pip-audit clean
- Test-change justifications: none (additions only)
- Completed: 2026-08-02

### S5 — LP discovery & valuation — `complete`
- Attempts: 1
- Branch: `section/s5-lp-discovery` | Merge commit: — | Tag: —
- Reference docs read: concentrated-liquidity-math--summary.md (formulas implemented
  verbatim); aerodrome-slipstream gauges guide (Voter recipe + depositor caveat);
  aerodrome quick-ref (Voter addr); §0.1(a)/(d) re-read
- Probe evidence (Step 2b): probes/out/s5/discovery.json — FULL recipe live at pin
  49424350: Sickle verified, tokenId 5056427 found via sickle on second NFPM,
  factory().getPool() resolved the known pool (no hardcoding), gauge path exercised
  (empty as expected), CL math independently reproduced position value (~$16.7k;
  5.027638 WETH + 7423.73 USDC @ $1845.72)
- Design notes / decisions: pool resolution generalized via factory(); sickle
  owner-mismatch exclusion; unresolved pool -> flagged, never mis-valued
- Test summary (written before code): 17 tests — CL math anchored to live-verified
  constants + hypothesis invariants (monotonic delta, non-negative, boundary
  continuity), discovery replay of live raws, custody rules, recording, CLI;
  +3 edge-path tests post-impl (gauge dedup, pool-unresolved, unknown decimals)
- Verifier verdict: "VERDICT: PASS" — fresh agent, clean clone of 3198e21, make test
  PASS (224 passed; "Required test coverage of 80% reached. Total coverage: 95.15%"),
  make audit PASS; LIVE SMOKE: real position discovered + recorded end-to-end
  (4.89 WETH + 7677.85 USDC @ $1851.08, in range, custody sickle)
- Coverage: 95.15% total; clmath 100%, discovery 93% | ruff/mypy/bandit/pip-audit clean
- Test-change justifications: feat commit 3198e21 — below-range max corrected to
  formula-derived 9.23 WETH (the 9.01 in VERIFIED_FINDINGS §6 was a pre-ticks
  ESTIMATE; the suite now pins the live-verified band prices 1689.24/2063.22 that
  anchor the exact ticks). FLAG: VERIFIED_FINDINGS §6 delta figures (9.01 max) should
  be read as superseded by the formula-derived values.
- Completed: 2026-08-02

### S6 — Portfolio analysis & recording — `complete`
- Attempts: 1
- Branch: `section/s6-portfolio` | Merge commit: — | Tag: —
- Reference docs read: defi-portfolio best practices §2-3 (event lifecycle adopted);
  aggregation playbook (with §0.1 weakening); tax-tracking (raw state retention);
  aave-v3 guide (on-chain account read chosen over subgraphs)
- Probe evidence (Step 2b): probes/out/s6/portfolio.json — Aave v3 Base Pool verified
  live (collateral $12,432.32 / debt $4,673.04 / HF 2.2082, scalings 1e8+1e18+bps);
  native + WETH/USDC/AERO balances at same pin
- Design notes / decisions: lifecycle events derived from successive observed states
  (open/modify/full_close), S3-S5 record functions untouched; holdings pricing joins
  in S7/S8 (amounts only in S6)
- Test summary (written before code): 16 tests — Aave exact Decimals, holdings with
  zero-skip, lifecycle derivation incl. event-replay property, orchestrator
  idempotence (opened exactly once across two runs), report shape
- Verifier verdict: "VERDICT: PASS" — fresh agent, clean clone of 626dce5, make test
  PASS (240 passed; "Required test coverage of 80% reached. Total coverage: 94.89%"),
  make audit PASS; LIVE SMOKE: full portfolio snapshot (lp 1 / hedge 1 / defi 2 /
  holdings 4) + report with totals lp_value $16,740.76 / defi_net $7,759.28
- Coverage: 94.89% total; aave 100%, holdings 96%, lifecycle 90% | all static clean
- Test-change justifications: feat commit 626dce5 — AERO expectation corrected (raw
  at 18 decimals is dust; original test misread probe raw as whole tokens)
- Completed: 2026-08-02

### S7 — Hedge coverage engine — `complete`
- Attempts: 1
- Branch: `section/s7-hedge-engine` | Merge commit: — | Tag: —
- Reference docs read (SCHOOL RECONCILIATION): personal\insurance-policy--strategy.md
  — ADOPTED VERBATIM: quadrant model (Q3 profit zone, Q4 decision point), break-even-
  at-floor sizing (NOT total LP value), coverage% = notional/exposure, SL near Q1/Q2
  boundary rule (live SL $1925 = 63% -> flagged), premium/re-arm economics (engine
  exposes premium_if_sl_fires; ledger in S8+). encylopedia Uig\5.action items.pdf
  checked: Quest-5 FA checklist — strategy context, not hedge mechanics (noted per
  gate rule). concentrated-liquidity-math summary re-used (S5).
- Probe evidence (Step 2b): N/A — computes from recorded verified states; fixtures =
  live S5 LP + S4 short states
- Design notes / decisions: read-only engine; correlated top-exit/SL flag (VERIFIED
  §6); recommendations never clamp silently — flag when target exceeds S9 limits
- Test summary (written before code): 19 tests — school rules on live fixture
  (quadrants, coverage 1.40 over-hedged, break-even invariant |net|<$0.01 at floor,
  SL rules, premium), dual-curve simulate properties, limits flag, hypothesis
  coverage-non-negative
- Verifier verdict: "VERDICT: PASS" — fresh agent, clean clone of ef9ced1, make test
  PASS (264 passed; "Required test coverage of 80% reached. Total coverage: 95.11%"),
  make audit PASS; LIVE: hedge status on fresh market data — quadrant Q3, coverage
  1.476, flags exactly [over-hedged, sl-correlated-with-top-exit, sl-below-q1q2-rule],
  S* = 6.778 ETH, premium-if-SL -$393.49; simulate at floor: net +$46.80 (over-hedge
  cushion visible)
- Coverage: 95.11% total; engine 98% | ruff/mypy/bandit/pip-audit clean
- Test-change justifications: feat commit ef9ced1 (precision-artifact assertion fixes
  only; contract unchanged)
- Completed: 2026-08-02

### S8 — Reporting & alerts (ntfy) — `complete`
- Attempts: 1
- Branch: `section/s8-alerts` | Merge commit: — | Tag: —
- Reference docs read: ntfy--api-reference.md (POST to topic; Title/Priority/Tags;
  topic = secret by knowledge-of-name; best-effort delivery, rate limits ->
  cooldown per state-change); python-scheduling--playbook--windows.md (Task
  Scheduler drives CLI: hourly snapshot + 5-min alerts check; catch-up,
  no-new-instance, hang guard; nonzero exit = scheduler history as health log)
- Probe evidence (Step 2b): pending — one live low-priority publish, receipt
  dumped to probes/out/s8/publish_receipt.json (topic redacted)
- Probe evidence (Step 2b): probes/out/s8/publish_receipt.json — live publish to
  the real topic HTTP 200, receipt shape pinned (id/time/expires/event/priority/
  tags; topic redacted in fixture); receipt is the gate-suite mock
- Design notes / decisions: rules engine over recorded state only (7 rules:
  naked-lp, price-near-sl, near-band-edge, rebalance-needed, snapshot-stale,
  quota-critical [monthly credit budgets only — rate windows fill transiently],
  healthcheck-degraded); JSON publish to `/` with endpoint label `publish` so the
  topic never reaches URLs/api_call_log/errors; record-before-deliver so failures
  are never lost (exit 3 degraded); cooldown dedup via alerts_log counts
  undelivered rows too (no retry storms); `_latest_hedge_inputs` moved to
  hedge/state.py (shared by hedge status + alerts); ntfy provider seeded 30/min
- Test summary (written before code): 31 tests — each rule fires on violation /
  silent when healthy, cooldown suppression + expiry, record-then-mark-delivery,
  priority name→level mapping, action-button forwarding, topic hygiene (error
  text, api_call_log, repr, CLI output, alerts_log), CLI e2e on live fixtures
  (fire→deliver→log, 500→degraded-recorded, dry-run inert, no-topic degraded)
- Spec: docs/specs/S8-alerts.md
- Verifier verdict: "VERDICT: PASS" — fresh agent, clean clone of 95c2e80, make test
  PASS (295 passed; "Required test coverage of 80% reached. Total coverage: 94.98%"),
  make audit PASS; adversarial review: no defects (cooldown boundary, UTC ts ordering,
  record-durability via autocommit, topic hygiene incl. raw grep of live data dir all
  verified); LIVE SMOKE: real snapshot then alerts check --dry-run fired exactly
  rebalance-needed (short 7.0386 vs target 4.7640 ETH, Q3, $1,856.27); alerts test
  landed a REAL notification on the topic (receipt by8NSR8zr6C1); `git grep` for the
  topic string empty repo-wide
- Coverage: 94.98% total; rules 99%, ntfy 95%, hedge/state 93% | all static clean
- Test-change justifications: post-tests-first edits were lint/type-only (unused
  unpack -> _out; dict -> Mapping params); assertions and contract unchanged
- Completed: 2026-08-02
