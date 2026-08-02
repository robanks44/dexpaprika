# PROGRESS.md — Loop State (single source of truth)

> Keep this file accurate after every step. Assume the next loop iteration runs in a
> fresh session with no memory. Never mark a section complete unless the fresh-agent
> verifier reported all gates passing.

## Status

- Phase: `building`  <!-- not-started | setup | building | complete | blocked -->
- Current section: S2.5 — provider quota tracker — `in_progress` (started 2026-08-02)
- Last updated: 2026-08-02
- Blockers: none (open items below are not blockers for S1–S5)

## Git state (mirror of GIT_RULES.md expectations)

- Last completed tag: s2-complete | main tip at last update: the s2 merge commit (see `git log`)
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

### S2.5 — Provider quota tracker — `in_progress`
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
- Test summary (written before code): —
- Verifier verdict: —
- Coverage: — | ruff: — | mypy: — | bandit/pip-audit: —
- Test-change justifications: none
- Completed: —
