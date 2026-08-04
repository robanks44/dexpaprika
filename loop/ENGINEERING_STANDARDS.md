# ENGINEERING_STANDARDS.md — Non-Negotiable Build Standards

**Version 2.0** — revised 2026-08-01 against verified research and live probes. Changes
from v1 are marked **[v2]**. These standards apply to every section of the dexpaprika
project. The loop's verification gates check against this file. "Best practice or higher"
is the floor, not the target.

**[v2] Reference paths:** all `Context Docs\…` paths in v1 are DEAD — that folder no
longer exists. Docs now live in the loop-maintained library at
`C:\Users\NoBloat\COWORK\CONTEXT\reference\`, resolved via its `INDEX.md`.

## 0. Operating model

- **Claude is the operator, not Richard.** Every capability is exposed through a single
  CLI entrypoint with `--json` machine-readable output, meaningful exit codes, and a
  `status`/`healthcheck` command. No feature may exist that only works through a human UI.
- **Read-only by default.** The system observes, records, and analyzes. Anything that can
  move funds or alter a live position is a privileged action (see §4).
- **[v2] Design the command surface for an agent.** Prefer a few coarse, well-named
  commands over many fine-grained ones; consolidate related reads into one call rather than
  forcing round-trips; errors state what to fix, not opaque codes. `simulate`/`status` and
  `execute` are SEPARATE commands with separate scopes — never flags on one command.
- **[v2] Safety must not depend on the operator behaving correctly** (see §4 kill switch).

## 1. Language, tooling, and layout

- **[v2] Python 3.13** pinned (v1 said 3.12+). 3.14 exists but is less shaken out; the
  binding constraint is web3/DeFi library support — verify the dependency matrix before
  committing and record the decision.
- `src/` layout per `reference\python--best-practices--project-structure.md`.
- Dependency and env management: `uv` (lockfile committed). Pinned, reproducible installs.
- Static gates every section: `ruff` (lint + format), **`mypy --strict`** (mypy 2.0 shipped
  May 2026 — still the correct gate). **[v2]** Astral's `ty` is faster but Beta as of
  July 2026: advisory-only, never the gate. No `# type: ignore` without an inline
  justification comment.
- All external data crosses the boundary through `pydantic` models — no raw dicts past the
  client layer.
- **[v2] Money math.** All money/price/quantity arithmetic in `Decimal` (or integer base
  units), never float. **Parse from strings — never `float()` an amount** (Hyperliquid and
  several others return numerics as JSON strings). Set an explicit `decimal.Context`; fix
  the rounding mode deliberately and match it to each venue's own tick/lot rounding. Never
  mix `Decimal` and `float` in one expression.
- Structured logging (`structlog` or stdlib JSON logging): machine-parseable, no secrets in
  logs, correlation id per run.

## 2. Reliability

- Every network call has an explicit timeout, retry with exponential backoff + jitter
  (`tenacity`), and a circuit breaker per upstream. **[v2]** Starting values carried from
  the prior project's hardening work: ~0.6 s inter-request delay, 30 s timeouts, 10 MB
  response caps, HTTPS enforced.
- **[v2] Client-side rate limiting per provider, config-driven.** Implement the
  multi-provider quota tracker described in `REFERENCE_INDEX.md` §3b: per-provider config
  carrying base URL, rate limit + period, credit budget, per-endpoint credit weight and
  tier limits, backed by a queryable call log — adding a provider is config, not code. It
  must handle weighted-credit providers (Krystal: positions cost 10; CoinStats: 400/DeFi
  call) and flat providers (DexPaprika: 1 request = 1 credit) as first-class cases.
- DexPaprika free tier: **30 req/min** — enforce it (200k req/mo without a key, 500k with).
- Fallback data sources where feasible (DexPaprika → DexScreener/CoinGecko) with the
  source recorded on every stored datapoint.
- **[v2] Cache-freshness is a correctness concern, not a performance one.** Any upstream
  offering a force-refresh parameter MUST use it on reads that feed a calculation — Zerion
  requires `sync=true` or it serves a snapshot that can be weeks stale with no staleness
  indicator whatsoever (see REFERENCE_INDEX §0). Record `as_of` per datapoint and treat an
  unknown-age read as unusable for hedge sizing.
- Jobs are idempotent and resumable; a crashed run can be re-run without corrupting data.
- SQLite in WAL mode, foreign keys on, versioned migrations, automated backup + a tested
  restore path, `PRAGMA integrity_check` in healthcheck.
- **[v2] Block-pinned snapshots.** Every on-chain read in one snapshot uses the SAME block
  per chain (resolve `eth_blockNumber` minus a small reorg margin, then pass
  `block_identifier` everywhere). Include `Multicall3.getBlockNumber()` in each batch and
  assert it equals the pin — a free tripwire against a load-balanced RPC serving a lagging
  node. Store the block number with the snapshot. Off-chain sources with no block (e.g.
  Hyperliquid) carry a timestamp; if skew against the chain pin exceeds a few seconds,
  DISCARD the snapshot rather than computing a coverage ratio from it.
- `healthcheck` verifies: DB integrity, migrations current, upstream reachability, secrets
  present, clock sanity, last-snapshot age. **[v2]** and reports the operational state the
  agent must self-check before acting: dry-run vs armed, kill-switch state, current
  exposure vs configured limits. Exit 0 only if all pass.

## 3. Security and supply chain

- **Secrets:** OS keyring locally (`reference\python-keyring--setup--windows.md`),
  environment variables / secret manager in cloud. Never in code, config files, logs, or
  git. `.env` is gitignored; secret-scanning (`gitleaks` or `detect-secrets`) runs in
  pre-commit and CI. **[v2] Migration path:** OS keyring → `sops`+`age` when secrets must
  travel with the repo/CI → cloud secret manager with workload identity federation. The
  shape stays constant (one named secret, injected at runtime, never in code), which is
  what makes the migration cheap.
- API keys are least-privilege and read-only wherever the provider allows. **[v2]**
  Withdrawal permissions disabled on any exchange key; separate keys per function.
- **[v2] Dependency hygiene.** `pip-audit` **and** `osv-scanner` clean (or documented,
  accepted findings) as a section gate, run on every change **and on a schedule** — new
  CVEs land after you pin. `bandit` clean. Generate a **CycloneDX SBOM** on release.
  Lockfile pins exact versions with hashes; updates deliberate, never implicit.
- **[v2] Provenance is not safety.** 2026 saw coordinated worm campaigns across PyPI and
  npm, including packages **validly Sigstore-signed but backdoored** via a compromised CI
  identity. A green attestation proves origin, not intent. Any dependency touching
  credentials, subprocess or network I/O on the order path gets reviewed by hand.
- **[v2] eth_defi has a 100+ transitive dependency surface.** The prior project cut this
  ~90% by extracting only the needed surface behind adapters. Consume it narrowly — import
  the minimum, audit what it pulls in, consider vendoring the small surface actually used.
  Document the decision either way.
- All input validated at every boundary (CLI args, API responses, DB reads); parse, don't
  validate-later.
- **[v2]** v1's "meets or exceeds `Context Docs\SECURITY_POLICY.md`" is removed — that file
  was a VS Code workstation path-restriction policy, is parked in `CONTEXT\_to_delete\`,
  and is comprehensively exceeded by this section. Its one transferable idea is retained:
  the system reads and writes only within its configured data directory.

## 4. Privileged actions (hedge adjustments / anything transacting)

If and only if scope includes changing the GMX position programmatically:

- `--dry-run` is the DEFAULT for every mutating command; live execution requires an
  explicit `--arm` flag AND an armed-state file created in a separate step.
- Hard limits in config: max position size, max delta adjustment per run, max daily
  adjustments, allowed markets. **[v2]** Enforced in code before the request reaches the
  client layer — not only as a venue-side setting. Plus an order-submission rate limit
  independent of the venue's, to bound the blast radius of a runaway loop.
- **[v2] Idempotency keys on every order-placement call**, derived deterministically from
  the system's own decision identity (strategy + signal + time bucket) so a
  crash-and-restart cannot double-fire. Store the first response and replay it verbatim on
  retry, with bounded expiry (Stripe's model is the reference).
- Simulate before send; verify post-conditions after send. **[v2]** Write the audit record
  **before** attempting the action, and record blocked and rejected attempts with reasons,
  not just executions. Append-only: intent → simulation → submission → confirmation.
- **[v2] A kill switch the agent cannot override.** Halts all mutating behaviour, checked
  before every privileged action, trips on P&L drawdown and anomaly signals (N consecutive
  failed orders, unexpected balance delta), and **requires manual re-arm — no auto-resume**.
  Enforcement must not live solely in the code path the agent controls.
- **[v2] Confirmation gates must be substantive.** A prompt satisfiable with a bare "yes"
  is not a control: any above-threshold action requires the agent to restate its reasoning
  and the data it relied on.
- **[v2]** Review the design against the **OWASP Top 10 for Agentic Applications (2026)**
  before this section ships — particularly Tool Misuse, Identity/Privilege Abuse, Cascading
  Failures, Human-Agent Trust Exploitation, and Rogue Agents. Read the official document.
- Signing keys, if ever held, live in the OS keyring / cloud KMS, never on disk in plain
  form — and this scope requires Richard's explicit approval first.

## 5. Testing (the autonomy requirement)

- **Tests are written before implementation for every section** (see LOOP_PROMPT.md).
- The full suite runs offline with zero human action: HTTP mocked via `respx`/`responses`
  with recorded fixtures (`vcrpy` cassettes where hand-writing shapes is impractical); time
  frozen via `freeze-time`; network blocked in unit tests (`pytest-socket --disable-socket`);
  temp DBs per test. See `reference\pytest--best-practices.md`.
- **[v2] Fixtures come from real recorded payloads**, not hand-invented ones — the probe
  dumps in `Defi_Tracker_3.0\probes\out\` and VERIFIED_FINDINGS payloads are the source.
  A fixture that never matched a real response tests nothing.
- Unit + integration tests per section; property-based tests (`hypothesis`) for hedge/LP
  math. **[v2] Invariants worth property-testing:** value conservation across a transaction
  (in = out + fees); rounding neither creates nor destroys value (sum of rounded parts =
  rounded total); idempotent re-application of an event yields the same state; position
  sizing never exceeds configured limits for any generated input; hedge coverage never
  negative; IL model monotonicity.
- A separately-marked `live` smoke suite (read-only, public endpoints) exists but is
  excluded from the gate — the gate suite must pass with no network and no secrets.
- Coverage gate: ≥90% on core logic (hedge math, data layer), ≥80% overall.
- Tests may not be weakened to pass. Changing an existing test requires a logged
  justification in PROGRESS.md.

## 6. Cloud migratability

- 12-factor: all config via `pydantic-settings` (env-first), no machine-specific paths in
  code, one artifact runs locally and in a container unchanged.
- Multi-stage Dockerfile, non-root user, pinned base image; `docker compose` for local
  parity; documented TimescaleDB/Postgres migration path for the time-series tables
  (`reference\timescaledb--api-reference--lp-tracker.md`).
- Scheduling is externalized (Task Scheduler / cron / cloud scheduler calls the CLI) — no
  in-process daemon required for **correctness**. See
  `reference\python-scheduling--playbook--windows.md`.
- **Amendment 2026-08-04 (daemon vs correctness):** a long-running **recorder service**
  IS required for **liveness** — the live dashboard's real-time view and the tick-driven
  alert engine (S12/S13) exist only while it runs. This does NOT relax the rule above:
  **correctness** (recording, healthcheck, backfill) must stay achievable via plain CLI +
  external scheduler, which is the gap-filling fallback when the service is down. Net:
  the daemon buys liveness; scheduled CLI snapshots remain the correctness backstop. A
  dead recorder must be caught by the EXTERNAL dead-man's-switch (S13), never fail silent.

## 7. Version control

- Everything is tracked in git automatically, per `GIT_RULES.md` (binding): bot identity,
  .gitignore-first init, pre-commit secret scanning, `main` always verifier-green, section
  branches, conventional commits at every loop gate, annotated `s<N>-complete` tags, no
  force-push/history rewrites, clean tree at the end of every iteration.
- Secrets can never enter history; if one does: rotate first, purge second, log third.
- An optional private remote (Richard-approved) is pushed automatically after each merge.
- **[v2] Reference link-integrity check** in pre-commit or `make test`: every path cited in
  `REFERENCE_INDEX.md` must resolve. A broken reference fails loudly rather than rotting.
  (v1 shipped with four dead `Context Docs\` paths that survived unnoticed — this check is
  the fix.)

## 8. Documentation

- `RUNBOOK.md` maintained as sections complete: every CLI command, every failure mode and
  recovery step, written so a fresh Claude session can operate the system from it alone.
- `ARCHITECTURE.md` kept current; ADR-style notes for consequential decisions.
- **[v2]** Record in `PROGRESS.md`, per section, which references were read and any place
  a local mirror disagreed with the live source — that record is what keeps
  `REFERENCE_INDEX.md` honest over time.

---

## Appendix — v2 revision summary

| Area | v1 | Verified reality (2026-08-01) |
|------|----|-------------------------------|
| Reference paths | `Context Docs\…` | **Dead folder.** → `CONTEXT\reference\` via its INDEX.md |
| Python pin | 3.12+ | **3.13** |
| Typing gate | mypy --strict | unchanged, confirmed (mypy 2.0); `ty` Beta, advisory only |
| GMX Python SDK | assumed viable | **No official Python SDK.** Official is TypeScript `@gmx-io/sdk`; community `gmx_python_sdk` is reference-only |
| Dep audit | pip-audit + bandit | + `osv-scanner`, scheduled re-runs, CycloneDX SBOM, provenance≠safety |
| Order safety | dry-run, limits, kill switch | + idempotency keys, agent-non-overridable kill switch with manual re-arm, substantive confirmation gates, OWASP Agentic review |
| Money math | Decimal | + parse from strings, explicit context, venue-matched rounding |
| Snapshots | (unspecified) | **Block-pinned per chain**, with a Multicall3 block tripwire |
| Cache freshness | (unspecified) | **A correctness gate** — Zerion needs `sync=true` or serves silently stale data |
| Security baseline | defer to SECURITY_POLICY.md | stated directly here; that file is parked |
