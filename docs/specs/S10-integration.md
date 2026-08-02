# S10 spec — Whole-system integration & runbook

## Purpose

Make the system operable by a fresh Claude session alone: complete the
healthcheck (every §2 check real), prove the sections work TOGETHER
(cross-section integration tests + failure drills), give the operator a
live read-only smoke suite, keep the docs honest mechanically (link/command
integrity inside `make test`), and finish RUNBOOK.md (failure modes +
recovery for every subsystem).

S9 was NOT built (execution stays gated on Richard's explicit go-ahead), so
S10 integrates S1–S8; `operational_state` must report execution disabled.

## Reference gate

- `ENGINEERING_STANDARDS.md` §2 (re-read): healthcheck verifies DB
  integrity, migrations current, upstream reachability, secrets present,
  clock sanity, last-snapshot age, **[v2]** + operational state (dry-run vs
  armed, kill-switch, exposure vs limits). Exit 0 only if all pass.
- `LOOP_PROMPT.md` Step 8: whole-system check = fresh-agent full suite +
  `healthcheck` + read-only live smoke suite — all must pass.
- `python-scheduling--playbook--windows.md` (S8): nonzero exit ⇒ scheduler
  history is the health log — healthcheck must stay exit-code-honest.
- No new external upstream ⇒ no new probe fixtures; integration tests run
  on the S3–S8 probe-recorded fixtures, and the live smoke suite IS the
  live component (done criteria).

## Healthcheck completeness (`dexpaprika healthcheck`)

The five `not-implemented` checks become real; the healthcheck now NEEDS
NETWORK for two of them (it is an operational command, not part of the
offline gate — gate tests mock the transport factory):

| check | implementation | fail means |
|---|---|---|
| `upstream_reachability` | one cheap live call per upstream: Base RPC + Arbitrum RPC `eth_blockNumber` (ring failover as everywhere), GMX `/markets`, DexPaprika `/networks` — all quota-gated via the shared transport; ntfy deliberately excluded (publish costs a message; `alerts test` covers it) | that upstream (named) is down/blocked |
| `clock_sanity` | Base `eth_getBlockByNumber("latest")` timestamp vs local UTC clock; abs skew > 5 min fails (piggybacks the reachability client — no extra quota beyond one call) | local clock wrong ⇒ staleness/cooldown/quota windows are lies |
| `last_snapshot_age` | newest `snapshots.ts` age vs `snapshot_staleness_minutes` (same rule as the S8 alert; no snapshots at all = fail with "run `dexpaprika snapshot`") | recorder pipeline gapped |
| `repo_state` | if a `.git` checkout is present: `git status --porcelain` must be empty (dirty tree = unverified code running); no `.git` (installed wheel) = ok with note; git binary absent = ok with note | operator is running uncommitted code |
| `operational_state` | reports mode: S9 absent ⇒ `read-only (execution not built; S9 gated)`; limits echoed (`max_position_usd` etc., all 0 = execution disabled — that IS the safe state); fails only if an `orders` table row exists with status not in a terminal state (nothing should be placing orders) | an order exists in a system with no executor |

`healthcheck --json` keeps the existing contract: `checks` map, exit 0
only when ALL pass, else 3. Message text stays actionable (what to fix).

## Cross-section integration tests (offline gate)

`tests/test_integration.py` — the full operator lifecycle against the
recorded probe fixtures, one flow per test:

- **Cold start→analysis:** migrate → wallets add → `snapshot` (all kinds)
  → `report` → `hedge status` → `alerts check` → `healthcheck` (mocked
  transports) — every exit 0, healthcheck all-ok, alert fired+delivered.
- **Failure drills:**
  - dead RPC (every ring peer 5xx): `snapshot --kind lp` exits 1 with an
    actionable error; nothing partial recorded for that kind.
  - stale recorder: age > threshold ⇒ `healthcheck` exit 3
    (`last_snapshot_age` fail) AND `alerts check` fires `snapshot-stale`.
  - quota exhausted (monthly credit budget seeded full): client call
    fails fast with the budget error; `alerts check` fires
    `quota-critical`.
  - clock skew: block timestamp 10 min from local ⇒ `clock_sanity` fail.
  - backup/restore drill: `db backup` → corrupt live DB → `db restore`
    → `db status` integrity ok and data intact.
- **Doc/link integrity (wired into `make test`):**
  - every ` ```…``` `-block `dexpaprika …` command line in RUNBOOK.md
    parses against `build_parser()` (docs can't drift from the CLI);
  - every relative repo path referenced in RUNBOOK.md + docs/specs/*.md
    exists;
  - every CLI subcommand has a RUNBOOK mention (nothing undocumented).

## Live read-only smoke suite (out of gate)

`tests/live/test_smoke_live.py`, marked `live`; gate excludes it via
`-m "not live"` in addopts. `make smoke` runs it with sockets enabled
against a throwaway data dir: healthcheck (all real), snapshot all kinds,
report, hedge status, alerts check --dry-run. Read-only: records only to
its own temp DB, sends nothing. This is the Step 8 whole-system live leg.

## RUNBOOK completion

- New "Failure modes & recovery" section: dead RPC/peer failover, circuit
  breaker open, quota budget exhausted, stale recorder, DB corruption →
  restore drill, secret missing, clock skew, degraded healthcheck triage
  order.
- Healthcheck reference table (check → meaning → recovery command).
- Fresh-session bootstrap: the read-order (S8) promoted to the top of the
  RUNBOOK with healthcheck-first triage.

## Standards obligations

- No new deps. mypy --strict; coverage ≥80% total (≥90% new code);
  subprocess use for `repo_state` is fixed-argv, shell-free, absolute-path
  resolved (bandit-clean with inline justification).
- Healthcheck stays read-only; smoke suite read-only by construction.

## Done criteria

Gate green + fresh-agent verifier PASS, and LOOP_PROMPT Step 8
whole-system check run by the verifier: full suite + REAL `healthcheck`
(all checks pass live) + the live smoke suite green against Richard's
wallet. RUNBOOK complete enough that the verifier confirms it could
operate the system from RUNBOOK.md alone.
