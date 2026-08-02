# S6 spec — Portfolio analysis & recording jobs

## Purpose

The unified portfolio layer: Aave v3 account (defi group), wallet holdings
(holdings group), append-only lifecycle events derived from successive
observations, the `snapshot` orchestrator, and the `report` command.

## Probe evidence (Step 2b — 2026-08-02, `probes/out/s6/portfolio.json`)

- Aave v3 Base Pool `0xA238Dd80C259a72e81d7e4664a9801593F98d1c5` verified by
  live `getUserAccountData(wallet)`: collateral $12,432.32, debt $4,673.04,
  HF 2.2082 (consistent with the known ~2.19, drifted) — scalings confirmed
  (base currency 1e8, HF 1e18, thresholds bps).
- Holdings at the same pin: native 0.0969 ETH, WETH dust, 96.71 USDC,
  3.099 AERO — `eth_getBalance` + `balanceOf` raws dumped as fixtures.

## Public interface — `dexpaprika.portfolio`

- `aave.read_account(rpc, wallet, block) -> AaveAccount` (exact Decimals);
  `aave.record(conn, wallet, account, ts)` → TWO position rows (kind
  `lend` = collateral, kind `borrow` = debt), venue `aave-v3`, group
  `defi`, HF in every state (Richard: lend/borrow grouped under defi).
  Pool address from `Settings.aave_pool_base`.
- `holdings.read(rpc, chain, wallet, block) -> list[Holding]` — native +
  per-chain token registry (Base: WETH/USDC/AERO; Arbitrum: WETH/USDC);
  zero balances skipped. `holdings.record(...)` → kind `holding`, group
  `holdings`, venue `wallet`, external_id `chain:symbol`.
- `lifecycle.observe(conn, position_id, ts)` — compares the newest
  `observed` state with the previous one and appends the derived
  transition event: none-prior → `open`; tracked metric changed
  (`liquidity` / `size_tokens` / `amount` / `total_collateral_usd`) →
  `modify` with `{field, old, new}` delta; unchanged → nothing extra.
- `lifecycle.reconcile_closures(conn, wallet, venue, kind, present_ids,
  ts)` — previously-open rows absent from the current sweep get a
  `full_close` event + `closed_at` (once; idempotent).

## CLI

```
dexpaprika snapshot [--kind lp|hedge|defi|holdings|all] [--address] --json
dexpaprika report --json
```

- `snapshot` orchestrates the section clients (S3–S5 code untouched),
  derives lifecycle events after each recording, reconciles closures,
  writes `snapshots` rows; idempotent and resumable (re-run = observed
  rows appended, no duplicates, closures not re-fired).
- `report` renders the latest state per open position grouped
  `lp_hedge` / `defi` / `holdings` with per-datapoint `as_of` + source,
  USD totals where states carry them (LP value, Aave collateral/debt);
  holdings show amounts (pricing joins in S7/S8).

## Standards obligations

- Event stream append-only; observed + transition events never mutated.
- One pinned block per chain per run; Decimals throughout.
- Coverage ≥90% on lifecycle (core logic).

## References read (Step 2)

defi-portfolio--best-practices.md §2–3 (unified model + event lifecycle —
adopted; §4/§9/Redis out of scope per Richard);
defi-position-aggregation--playbook.md noted with §0.1 weakening;
defi-tax-tracking--best-practices.md (record-keeping: raw states retained);
aave-v3--integration-guide.md (account-data read; subgraph paths not used —
on-chain read chosen, probe-verified).
