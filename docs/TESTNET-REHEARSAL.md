# S9 testnet rehearsal — Arbitrum Sepolia (run on your Windows machine)

Goal: exercise the FULL live execute path — arm → order → phone approval →
submit → verify — against GMX's Arbitrum Sepolia testnet, where nothing is
real money. Once this works cleanly, the same steps run on mainnet from the
cloud. Paste me each command's output; I check it and give go/no-go before
the next step.

Why Arbitrum Sepolia (not Base): GMX perps only exist on Arbitrum and
Avalanche. Base has no GMX order contracts. Your LP is on Base; your hedge
is on Arbitrum — `execute` only ever touches the Arbitrum side.

---

## 0. One-time local setup

```
# in the repo root (unzip of dexpaprika-s9.5)
uv sync
cd executor && npm ci && cd ..
uv run dexpaprika --version
```

Needs Python 3.13, uv, and Node.js. `--version` printing confirms the CLI runs.

## 1. Testnet wallet + faucet ETH

- Use a wallet you're happy to use for testing (can be your usual address —
  testnet funds are worthless). Note the address.
- Get Arbitrum Sepolia ETH from a faucet (e.g. an Arbitrum Sepolia faucet, or
  bridge Sepolia ETH). You need a little for: opening a test position, the
  One-Click authorization tx, and funding the subaccount for keeper fees.

## 2. Open a tiny test short + stop-loss on GMX testnet

- Go to the GMX **testnet** app (Arbitrum Sepolia), connect the wallet.
- Open a small ETH/USD **short** (smallest size the UI allows).
- Add a **stop-loss** order to it (any trigger above the mark).
- Confirm the SL exists. This is the order the rehearsal will nudge.

## 3. Enable One-Click Trading (creates the subaccount)

- In the GMX testnet UI, enable **One-Click Trading** (account menu). Approve
  the wallet transaction — this authorizes + funds a subaccount.
- FLAG: if the testnet UI doesn't offer One-Click Trading, stop and tell me —
  we switch to the build-a-helper path for testnet. Don't force it.

## 4. Extract the subaccount key into an env var

- In the browser on the GMX testnet tab: DevTools (F12) → Application → Local
  Storage → the GMX origin → find the One-Click / subaccount entry. It holds
  the subaccount **private key** for your account+chain.
- Paste me the KEY NAME and structure (NOT the value) first — I confirm we've
  got the right entry, since GMX's storage format can change.
- Then set it (PowerShell), for this shell only:

```
$env:DEXPAPRIKA_SECRET_BACKEND = "env"
$env:DEXPAPRIKA_SECRET_GMX_SUBACCOUNT_KEY = "<subaccount private key>"
$env:DEXPAPRIKA_GMX_CHAIN_ID = "421614"
$env:DEXPAPRIKA_EXECUTION_ACCOUNT = "<your wallet address>"
$env:DEXPAPRIKA_SECRET_NTFY_TOPIC = "<your ntfy topic>"
```

(The key lives only in this shell's memory — it's never written to disk or git.)

## 5. Pre-flight (read-only, no arming)

```
uv run dexpaprika db migrate --json
uv run dexpaprika execute status --json
uv run dexpaprika execute set-sl-trigger --price <SL+1> --json
```

- `execute status`: armed false, kill_switch false, limits shown.
- `set-sl-trigger` (no `--arm`): DRY-RUN — it should find your testnet SL
  order and print the plan (new trigger, request id), submit NOTHING. Paste
  the JSON; I verify the order key + trigger scaling before we arm.

## 6. The live testnet nudge (armed)

```
uv run dexpaprika execute arm --ttl-minutes 15 --json
uv run dexpaprika execute set-sl-trigger --price <SL+1> --arm --json
```

- On the second command your phone gets an **urgent ntfy**: it restates the
  action, order, and new trigger, with an instruction id. Reply
  `approve <id>` on the topic to fire, or `reject <id>` / ignore to abort.
- On approval it submits via the subaccount, waits for the relay, then
  re-reads the order and verifies the trigger moved. Expected: `confirmed`.
- Then nudge it back:

```
uv run dexpaprika execute set-sl-trigger --price <original SL> --arm --json
```

## 7. Confirm the audit trail

```
uv run dexpaprika execute status --json        # armed state / counts
# (I'll also have you dump the audit_log rows to confirm the chain:
#  intent -> simulation -> submission -> confirmation for each nudge)
```

## What "good" looks like

- Dry-run printed a real plan and sent nothing.
- The armed nudge required your phone approval and only fired after it.
- The order's trigger actually moved on testnet, then moved back.
- audit_log shows the full intent→confirmation chain; no gaps.
- No ARMED/KILL-SWITCH surprises; `execute status` sane throughout.

Once this is clean, mainnet is the identical flow with
`DEXPAPRIKA_GMX_CHAIN_ID` unset (defaults to 42161), the real subaccount
key, and — per your plan — running from the cloud.
