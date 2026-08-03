# S9 mainnet SL-nudge — supervised live rehearsal (your Windows machine)

Goal: prove the live execute path end-to-end on your real GMX short, with the
safest possible action — move the stop-loss trigger **$1,925 → $1,926 → back
to $1,925**. It never opens, closes, or resizes a position; it's fully
reversible; and you approve the exact numbers on your phone before anything
fires.

Your real order: `0xc7c11d5c6267283c0605352adb0daefa0593f5c7707a534d71646ce8ea2ce642`
(ETH short, SL trigger $1,925). Chain/account default to Arbitrum One + your
wallet — no chain env needed. The subaccount key stays in your PowerShell env
only; paste me each JSON output (no key in it) and I check it before you go on.

---

## Step 0 — Environment (repo root)

```
cd "C:\Users\NoBloat\COWORK\PROJECTS\DexPaprika API\dexpaprika"
uv sync
cd executor
npm ci
cd ..
uv run dexpaprika --version
```

Paste me the `--version` line (and any errors). We don't continue until the
CLI runs.

## Step 1 — Mainnet DRY-RUN (sends nothing, no key yet)

```
$env:DEXPAPRIKA_SECRET_BACKEND = "env"
$env:DEXPAPRIKA_SECRET_NTFY_TOPIC = "<your ntfy topic>"
uv run dexpaprika db migrate --json
uv run dexpaprika execute status --json
uv run dexpaprika execute set-sl-trigger --price 1926 --json
```

- `execute status`: armed false, kill_switch false, limits shown.
- `set-sl-trigger` (no `--arm`): DRY-RUN — it should resolve your real SL order
  `0xc7c1…e642` and print the plan (new trigger 1926, request id), send
  NOTHING. Paste me the JSON; I confirm the order key + trigger scaling before
  we ever touch the subaccount.

## Step 2 — Enable One-Click Trading (Rabby) + get the subaccount key

- Connect **Rabby** to app.gmx.io (Arbitrum One).
- Account menu → enable **One-Click Trading**. Set a **tight action cap**
  (e.g. 5-10 actions) and a **short expiry** (e.g. 1 hour) — smallest that
  covers the nudge. Approve the tx in Rabby.
- Open DevTools (F12) → Application → Local Storage → the GMX origin → find the
  One-Click / subaccount entry holding the **subaccount private key**.
- Paste me the key's NAME and JSON structure (NOT the value) — I confirm we've
  got the right entry (GMX's storage format can shift).
- Then set it (this shell only):

```
$env:DEXPAPRIKA_SECRET_GMX_SUBACCOUNT_KEY = "<subaccount private key>"
```

## Step 3 — The live nudge (armed, phone-approved)

```
uv run dexpaprika execute arm --ttl-minutes 15 --json
uv run dexpaprika execute set-sl-trigger --price 1926 --arm --json
```

- Your phone gets an **urgent ntfy** restating the action, order, and new
  trigger, with an instruction id. Reply `approve <id>` on the topic to fire,
  or `reject <id>` / ignore to abort.
- On approval it submits via the subaccount, waits for the relay, re-reads the
  order, and verifies the trigger moved. Expected: `confirmed`.
- Paste me the JSON at each step.

## Step 4 — Nudge back + confirm

```
uv run dexpaprika execute set-sl-trigger --price 1925 --arm --json
uv run dexpaprika execute status --json
```

- Approve the return nudge the same way. SL back at $1,925.
- We dump the `audit_log` rows to confirm the full chain
  (intent → simulation → submission → confirmation) for both nudges.

## What "good" looks like

- Dry-run printed a real plan and sent nothing.
- Each armed nudge required your phone approval and only fired after it.
- The SL trigger actually moved on-chain, then moved back.
- audit_log shows the full intent→confirmation chain; no gaps.
- `execute status` sane throughout; no kill-switch surprises.

## If anything looks off

- Stop and paste me the output. Don't re-run a submit.
- To halt everything instantly: create a file named `KILL-SWITCH` in the data
  dir (`data\` under the repo root). Nothing mutating runs while it exists; it
  clears only when you delete it.
