# S9 mainnet SL-nudge — supervised live rehearsal (your Windows machine)

Goal: prove the live execute path end-to-end on a FRESH tiny test short you
open yourself, with the safest possible action — move that order's stop-loss
trigger up $1 and back. It never opens, closes, or resizes a position; it's
fully reversible; and you approve the exact numbers on your phone first.

We target the test order by its EXACT key (`--key`), never auto-detect —
because your wallet also has an unrelated existing $1,925 SL order and we must
not touch it. Chain/account default to Arbitrum One + your wallet. The
subaccount key stays in your PowerShell env only; paste me each JSON output
(no key in it) and I check it before you go on.

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

## Step 1 — Open a tiny test short + stop-loss on GMX

- On app.gmx.io (Arbitrum One, Rabby connected), open the SMALLEST ETH short
  GMX allows (~$5 if permitted, else its minimum).
- Add a **stop-loss** to it at any trigger above the mark — note the exact
  trigger price you set; call it `SL`.
- Tell me the `SL` price. I read your wallet live and give you back the exact
  **order key** for THIS new order (distinguished from the $1,925 one by its
  trigger). Call it `KEY`.

## Step 2 — Mainnet DRY-RUN on the test order (sends nothing, no key yet)

```
$env:DEXPAPRIKA_SECRET_BACKEND = "env"
$env:DEXPAPRIKA_SECRET_NTFY_TOPIC = "<your ntfy topic>"
uv run dexpaprika db migrate --json
uv run dexpaprika execute status --json
uv run dexpaprika execute set-sl-trigger --key <KEY> --price <SL+1> --json
```

- `execute status`: armed false, kill_switch false, limits shown.
- `set-sl-trigger` (no `--arm`): DRY-RUN — it prints the plan (new trigger
  SL+1, request id) for YOUR test order and sends NOTHING. Paste me the JSON;
  I confirm the key + trigger scaling before we touch the subaccount.

## Step 3 — Enable One-Click Trading (Rabby) + get the subaccount key

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

## Step 4 — The live nudge (armed, phone-approved)

```
uv run dexpaprika execute arm --ttl-minutes 15 --json
uv run dexpaprika execute set-sl-trigger --key <KEY> --price <SL+1> --arm --json
```

- Your phone gets an **urgent ntfy** restating the action, order, and new
  trigger, with an instruction id. Reply `approve <id>` on the topic to fire,
  or `reject <id>` / ignore to abort.
- On approval it submits via the subaccount, waits for the relay, re-reads the
  order, and verifies the trigger moved. Expected: `confirmed`.
- Paste me the JSON at each step.

## Step 5 — Nudge back + confirm

```
uv run dexpaprika execute set-sl-trigger --key <KEY> --price <SL> --arm --json
uv run dexpaprika execute status --json
```

- Approve the return nudge the same way. Test order's SL back to `SL`.
- We dump the `audit_log` rows to confirm the full chain
  (intent → simulation → submission → confirmation) for both nudges.
- Optional cleanup: cancel the test order / close the tiny short in the GMX UI,
  and disable One-Click Trading when done.

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
