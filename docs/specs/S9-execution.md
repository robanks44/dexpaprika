# S9 spec — Hedge order execution (PRIVILEGED)

## Authorization record

- Richard's explicit build go-ahead: **"start s9"**, 2026-08-02.
- Decisions (AskUserQuestion, 2026-08-02): key custody = **GMX subaccount +
  official SDK sidecar** (chosen after live research: the community Python
  SDK has no SL/edit/cancel/subaccount support — the main-key path would
  still need raw calls while holding a full-power key); approval =
  **every live order approved via ntfy**; hard limits = **$20k max
  position / $5k max delta per run / 4 adjustments per day / ETH/USD
  only**; live exercise = **SL nudge with Richard supervising** after
  dry-run verification.

## Reference gate

- ENGINEERING_STANDARDS §4 in full (dry-run default, --arm + armed file,
  non-agent-overridable kill switch, hard limits pre-client, order rate
  limit, idempotency keys with stored-response replay, simulate-before-
  send + post-condition verify, audit BEFORE attempt incl. blocked and
  rejected, substantive confirmation gates, OWASP Agentic review).
- gmx-python-sdk--api-reference.md — evaluated and REJECTED for the
  write path (no orderType 6, no edit/cancel, no subaccounts).
- Official GMX SDK v2 docs (fetched at build): `@gmx-io/sdk` TypeScript;
  `executeExpressOrder` (stop-loss kind), `prepareEditOrder`
  (`newTriggerPrice` — the SL-nudge primitive), `prepareCancelOrder`,
  `submitOrder` with NATIVE `idempotencyKey`, `fetchOrderStatus` polling;
  subaccount lifecycle `generateSubaccount`/`activateSubaccount`
  (on-chain max action count + expiry); express mode relays via the GMX
  API (typed-data signatures, no executor gas management).
- ntfy--api-reference.md — approval loop WITHOUT an inbound server:
  publish action request, poll `/{topic}/json?poll=1&since=` for
  Richard's `approve <id>` / `reject <id>` reply.
- OWASP Top 10 for Agentic Applications 2026 — reviewed at build; see
  §OWASP below.

## Probe gate (Step 2b) — DONE, read-only

`probes/out/s9/sdk_fetch_orders.json`: pinned `@gmx-io/sdk@1.6.4` run
live from the sidecar against the real account — returned exactly the
recorded live SL order (`0xc7c1…e642`, orderType 6 StopLossDecrease,
short, triggerPrice 1925e12 — 1e12 ETH trigger scaling re-confirmed,
sizeDeltaUsd = uint256max full-close sentinel). SDK note: ESM build has
extensionless imports — the sidecar uses the CJS entry (probe-verified).

## Architecture (ARCHITECTURE §7)

**Python owns every safeguard; Node signs and submits.** The sidecar
(`executor/gmx_exec.cjs`, pinned lockfile committed) is a dumb tool: it
reads ONE JSON instruction on stdin (`{action, params, mode}`), talks to
the official SDK, writes ONE JSON result to stdout, exits. It holds no
policy, no retries, no schedule. Modes: `read` (fetch orders/status),
`prepare` (build + price/fee resolution — used by dry-run and
simulation), `submit` (sign with the subaccount key + relay; requires
the key env var, absent in every other mode).

Subaccount key: secret `gmx_subaccount_key` (keyring/env), generated at
the supervised setup, authorized by Richard's ONE wallet-signed
transaction (on-chain action count + expiry caps), renewable/revocable
in the GMX UI. It can trade the GMX account; it can never withdraw.

### `dexpaprika.execute` package

- `instruction.py` — typed `OrderInstruction` (pydantic, frozen):
  `action` (`set-sl-trigger` | `resize-short` | `cancel-order`),
  ETH/USD market constants, Decimal params, and
  `idempotency_key = sha256(action | canonical params | UTC hour
  bucket)` — deterministic decision identity (Stripe model), bounded
  expiry when the bucket rolls.
- `safety.py` — the gate chain, ALL enforced before the sidecar is ever
  invoked, each failure audited as `blocked`:
  1. kill switch — `KILL-SWITCH` file in the data dir, created by
     Richard or auto-tripped (recorded in audit_log) on: 3 consecutive
     failed submissions, or a post-condition mismatch. NEVER removed by
     code — manual re-arm only (standards: not solely agent-controlled;
     the CLI has no command that deletes it).
  2. armed state — live mode needs `--arm` AND the armed-state file
     (`ARMED`, created by the separate `execute arm` step, expires
     after `arm_ttl_minutes`); dry-run is the default everywhere.
  3. hard limits — max_position_usd 20000, max_delta_per_run_usd 5000,
     max_daily_adjustments 4 (counted from audit_log submissions),
     allowed_markets = ETH/USD — enforced in code, plus an order
     submission rate limit (min 60s between submissions) independent
     of venue limits.
- `approval.py` — every live order: publish ntfy request (priority
  urgent) with the SUBSTANTIVE reasoning — action, sizes, trigger,
  current analysis numbers, and the instruction id — then poll the
  topic for `approve <id>` / `reject <id>` from Richard (timeout
  `approval_timeout_minutes`, default 10; timeout = rejected).
  A bare "yes" cannot fire anything: approval is bound to the exact
  instruction id whose parameters were shown.
- `engine.py` — the pipeline: intent audit → gates → prepare (sidecar)
  → simulation audit → [dry-run STOPS HERE, printing the full plan] →
  approval → submission audit → sidecar submit (idempotency key) →
  poll status → post-condition verify (re-read orders/position; the
  change must match the instruction) → confirmation audit. Failures
  audit as `rejected`/`blocked` with reasons. Stored first response
  replayed verbatim on same-key retry.

### CLI (separate command scope — never mixed with reads)

```
dexpaprika execute arm [--ttl-minutes N] --json      # step 1 of two
dexpaprika execute set-sl-trigger --price P [--arm] --json
dexpaprika execute resize-short --target-eth E [--arm] --json
dexpaprika execute cancel-order --key 0x... [--arm] --json
dexpaprika execute status --json                     # armed/kill/limits/counts
```

Without `--arm`: full dry-run (prepare + plan + what WOULD be sent),
exit 0. `execute arm` refuses while the kill switch exists.

## OWASP Agentic Top 10 (2026) review — this design

- **ASI02 Tool Misuse**: sidecar accepts only the 3 typed actions with
  schema-validated params; hard param ranges enforced Python-side;
  submission rate limit. — mitigated.
- **ASI03 Identity/Privilege Abuse**: dedicated scoped subaccount key
  (never the user key, never shared), on-chain action count + expiry,
  keyring storage, revocable in UI; executor identity mapped to one
  human owner (Richard). — mitigated.
- **ASI08 Cascading Failures**: executor is a leaf (fan-out 1); circuit:
  3 consecutive failures trip the kill switch; scheduler never calls
  execute (alert → human → command). — mitigated.
- **ASI09 Human-Agent Trust Exploitation**: approval happens OUT-OF-BAND
  (ntfy on Richard's phone, not the chat/CLI that requested it), bound
  to the shown parameters; substantive reasoning required in the
  message. — mitigated.
- **ASI10 Rogue Agents**: named human kill switch (file Richard
  controls; no code path removes it); behavioral baseline = audit_log
  (every intent recorded before action; any submission without a prior
  intent row is test-asserted impossible); manual re-arm only. —
  mitigated.
- ASI01/04/05/06/07 noted: goal-hijack surface minimized because the
  executor takes no free-text input (typed CLI args only); supply chain
  pinned (`@gmx-io/sdk@1.6.4` + committed lockfile, `npm ci`); no
  agent-generated code executes; no inter-agent channel exists.

## Test focus (every safeguard BLOCKS — offline, sidecar mocked)

1. no ARMED file / expired / no --arm ⇒ blocked+audited (each).
2. kill switch present ⇒ blocked; `execute arm` refuses; auto-trip on
   3 consecutive failures writes the file + audit row.
3. each hard limit exceeds ⇒ blocked (position, delta, daily count,
   market, submission rate).
4. unapproved / rejected / timeout ⇒ no submission, audited rejected.
5. idempotency: same decision re-run ⇒ stored response replayed, sidecar
   NOT re-invoked; crash-after-submit restart ⇒ no double-fire.
6. audit completeness property: for every simulated submission there is
   a prior intent row (hypothesis over random pipelines); blocked and
   rejected paths always audited.
7. dry-run default: no --arm ⇒ sidecar called with prepare only; a
   `submit` call in dry-run is asserted impossible.
8. post-condition mismatch ⇒ kill switch trips + urgent alert.
9. sidecar contract: instruction JSON schema round-trip; subaccount key
   env passed ONLY in submit mode.

## Done criteria

- Gate + audit green; verifier PASS on a clean clone including LIVE
  READ-ONLY dry-run e2e: `execute set-sl-trigger --price 1926` (no
  --arm) against the real account — full plan printed, real order key
  resolved, NOTHING submitted, audit chain intent→simulation recorded.
- OWASP review logged (above).
- The armed live path ships UNEXERCISED and stays blocked by the
  missing subaccount authorization + missing ARMED file until the
  supervised session with Richard: subaccount generation + his one
  authorization tx + the $1 SL nudge through the full approval flow,
  then nudge back. That session is scheduled separately with him.
