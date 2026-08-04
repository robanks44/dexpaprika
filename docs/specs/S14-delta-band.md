# S14 — Delta-band rebalance hedge strategy (spec)

**Status:** in_progress (branch `section/s14-delta-band`)
**Depends:** S5 (live tick bounds — RESOLVED), S7 (`hedge.engine.analyze` delta target), S9
(privileged executor), S12a (recorder = fresh state to act on).
**Decisions (Richard, 2026-08-04):** (1) **auto-execute within hard limits** — resizes fire
without a per-trade phone approval; (2) band **7.5%** of max-delta to start, tuned later from
recorded rebalance data (never guessed); (3) **keep the widened SL as a backstop** — S14 does
not touch the SL. Library checked — the strategy encyclopedia is a fundamentals course, no
rebalance-band coverage; ground truth is the 2026-08-03 decision + VERIFIED_FINDINGS §6.

## North star (Richard, 2026-08-04)

**Every decision optimizes for the greatest net capital position.** Not max-safety, not
max-simplicity — max *net* capital, which explicitly penalizes fee churn, unhedged-delta
losses, AND catastrophic failure (a blow-up is the largest negative). Consequences baked into
this design:
- The rebalance decision rule is **expected-hedging-benefit > rebalance-cost**, not a fixed
  threshold. v1 uses a notional cost-floor as a PROXY (no volatility/cost data yet); S14
  records the attribution data so the real benefit-vs-cost rule is built from Richard's data.
- **Capital-optimal rollout = shadow → measure → tune → enable.** Auto-trading on un-tuned
  params bleeds capital, so S14 ships DORMANT (`auto_rebalance_enabled=False`): it evaluates +
  logs every would-be rebalance and its net-capital attribution in shadow mode; params are
  tuned to the settings that net the most capital; THEN live auto-execute is enabled. The
  guardrail numbers below are starting proxies to be replaced by the data-optimal values.
- Every executed/shadow decision is logged with enough to attribute realized net-capital
  impact (vs holding, vs the SL-ladder baseline) — the experiment journal is how we find the
  profit-maximizing strategy.

## Purpose

Replace the tight-SL ladder (root cause of the correlated failure: SL fires at the top-of-
range exit → short gone → price reverts → LP re-enters unhedged) with **delta-matched
rebalancing**: resize the GMX short to track the LP's live ETH exposure, triggered by delta
DRIFT, not price. The SL stays as a backstop during the learning phase.

Auto-execute posture: S14 does NOT bypass S9's safety pipeline. It reuses
`execute.engine.execute_instruction` with an **auto-approving** callback, so kill-switch,
armed-state, hard limits, audit-before, post-condition verify, and idempotency ALL still run.
S14 only supplies the "yes" automatically — and only when its own strategy gates pass AND
auto-rebalance is explicitly enabled (defense in depth: it cannot fire by default).

## Public interface

- `dexpaprika.strategy.rebalance`:
  - `evaluate(conn, settings, *, now) -> RebalanceDecision` — read-only. From the latest
    recorded state via `hedge.state.latest_inputs` + `hedge.engine.analyze`: `current_eth`
    (short size), `target_eth` (`delta_matched_target_eth`), `deviation` (|size−target|/
    max_delta), `band`, `band_breached`. Then the strategy GATES (all must pass to act):
    - `band_breached` (deviation > `hedge_rebalance_band`, 7.5%);
    - freshness — the hedge state is not stale (reuse S13 `assess_health` / snapshot age);
    - `min_interval` — ≥ `rebalance_min_interval_minutes` since the last executed rebalance
      (from `audit_log`/rebalance log) — anti-churn;
    - `cost_floor` — |target−current|·price ≥ `rebalance_min_notional_usd` (don't pay gas +
      keeper to move a trivial gap);
    - `daily_limit` — today's rebalance count < `max_daily_adjustments` (S9 hard limit);
    - `within_max_position` — target·price ≤ `max_position_usd` (S9 hard limit);
    - `auto_rebalance_enabled` — the explicit opt-in flag (see config).
    Returns `decision ∈ {"execute","hold","blocked"}`, `target_eth`, `current_eth`,
    `deviation`, `reason`, and `blocked_by: list[str]` (every failing gate, for transparency).
  - Net-capital attribution: `evaluate`/`run` persist each decision to a `rebalance_log`
    (migration `0004`): ts, decision, current_eth, target_eth, deviation, band, price,
    est_move_usd (|target−current|·price), gate states, executed?, executor idempotency key,
    and the newest snapshot id — enough to score realized net-capital impact from the recorded
    time-series later (shadow decisions included, so we can compare "would-have" vs holding).
  - `run(conn, settings, *, now, arm, sidecar=None) -> RebalanceOutcome` — evaluate, record
    the THESIS first (why + expected effect, per the operating philosophy), then:
    - if `decision=="execute"` AND `arm` AND `auto_rebalance_enabled`: build the
      `resize-short` `OrderInstruction(target_eth=…)` and run it through
      `execute_instruction(..., arm_flag=True, approval=_auto_approve)`. `_auto_approve`
      returns approved=True AND sends an ntfy NOTIFICATION ("auto-rebalanced short X→Y ETH,
      delta gap Z%") — informational, not a wait-for-reply gate.
    - otherwise: DRY-RUN — propose only, log the decision, execute nothing.
    Records the outcome (executed/blocked/held + the executor's result) after.
- CLI (`dexpaprika strategy …`):
  - `strategy status [--json]` — current delta gap, target vs current, deviation, band,
    due?, and every gate's state (offline; no execution).
  - `strategy rebalance [--arm] [--json]` — evaluate + (only with `--arm` AND
    `auto_rebalance_enabled`) auto-execute; DRY-RUN default. This is the scheduler entrypoint.
- Scheduler: an OPT-IN `strategy-rebalance` job (interval, `strategy_rebalance_minutes`) that
  runs `strategy rebalance --arm`. Added but its live effect is gated by
  `auto_rebalance_enabled` (default False) — installing the job does not arm auto-trading.

## Config additions

- `auto_rebalance_enabled: bool = False` — MASTER opt-in for auto-execution. False → S14 is
  propose/dry-run only, no matter what else is set. (Richard flips this on when ready.)
- `rebalance_min_interval_minutes: int = 60` — anti-churn floor between executed rebalances.
- `rebalance_min_notional_usd: Decimal = 250` — cost floor; smaller gaps are not worth fees.
- Reused S9 hard limits: `max_position_usd`, `max_daily_adjustments`, kill-switch.

## Behavioural rules

- **Every S9 guard still fires.** Auto-execute = auto-approval callback, nothing else
  bypassed. Kill-switch tripped → blocked. Not armed → blocked. Over a hard limit → blocked
  (and audited). Post-condition verify still confirms the new size ≈ target after submit.
- **Opt-in, defense in depth.** Live auto-execution needs BOTH `--arm` (+ S9 armed-state file)
  AND `auto_rebalance_enabled`. Default posture is dry-run/propose.
- **Anti-churn + cost-aware.** min-interval + cost-floor + daily-limit prevent fee-bleeding
  oscillation around the band edge.
- **Thesis before action (operating philosophy).** Every decision records why + expected
  effect BEFORE any attempt; the outcome is recorded after — the experiment journal is the
  point. Blocked/held decisions are logged too.
- **Honest + no fabrication.** Acts only on FRESH recorded state (stale → blocked, never act
  on stale data). Decimal money throughout. The target comes from `analyze()`, not a guess.
- **SL untouched.** S14 never modifies the stop-loss; it remains the backstop.
- **Notify, don't silently trade.** Every executed rebalance emits an ntfy notification.

## Tests (offline, written first)

1. `evaluate`: band breached + all gates pass → decision=execute with the right target;
   within-band → hold.
2. Each gate blocks independently: stale state, min-interval not elapsed, cost below floor,
   daily limit hit, over max-position, kill-switch tripped, `auto_rebalance_enabled` False —
   each → blocked with that gate in `blocked_by`; verify the gate that fires is named.
3. `run` dry-run (arm=False OR auto_rebalance_enabled False): proposes, executes NOTHING,
   logs the thesis + decision.
4. `run` auto-execute (arm=True + enabled + gates pass): calls `execute_instruction` with an
   auto-approving callback; a mocked executor confirms the resize instruction target; an ntfy
   notification is sent; thesis + outcome recorded.
5. Kill-switch / not-armed inside execute path → the executor blocks; S14 surfaces it, no
   trade; auto-approve callback is not the thing that bypasses these.
6. Anti-churn: two evaluations inside min-interval → the second is blocked by min-interval.
7. CLI: `strategy status` gate contract; `strategy rebalance` (no arm) dry-run exits 0 and
   trades nothing; `--arm` with `auto_rebalance_enabled` False still dry-runs.
8. Scheduler: `strategy-rebalance` job present (interval), and its argv is
   `["strategy","rebalance","--arm","--json"]`.

## Done-criteria

Full suite + ruff + mypy(strict) + coverage gate green; fresh-agent verdict PASS with EXTRA
scrutiny on the auto-execute safety (no guard bypassed; cannot fire by default; every gate
tested); no new runtime dep; `ARCHITECTURE.md` + `RUNBOOK.md` updated (auto-execute posture,
the `auto_rebalance_enabled` opt-in, guardrails); merge `--no-ff`, tag `s14-complete`.
