# S5 spec — LP discovery & valuation

## Purpose

Turn ARCHITECTURE §5.1 into code: discover CL LP positions for a wallet by
CUSTODY (wallet-held, Sickle-held, gauge-staked), read range/liquidity/pool
state at ONE pinned block, compute token amounts + USD price via CL math,
and record everything.

## Probe evidence (Step 2b — 2026-08-02, `probes/out/s5/discovery.json`)

Full recipe executed live at pin 49424350, all raw calls dumped as replay
fixtures:

- `SickleFactory.sickles(wallet)` → `0x6c1b2006…`; `owner()` == wallet ✓
- Enumeration: canonical NFPM `0x8279…`: 0 NFTs for both owners; second
  NFPM `0xa990…`: **sickle holds 1 → tokenId 5056427**, ticks
  −202000..−200000, liquidity 3987414535131380, tokensOwed 0/0
- **Pool resolution generalizes:** `NFPM.factory()` →
  `factory.getPool(token0, token1, tickSpacing)` → exactly the known pool
  `0x56aeaf…` (no hardcoded pool needed)
- Gauge path: `Voter.gauges(pool)` → real gauge `0xc1d2…`;
  `stakedValues` empty for both owners (NFT is Sickle-held, not staked) —
  path exercised, correctly empty
- Pool state at pin: tick −201114, price $1,845.72
- CL math cross-check: computed **5.027638 WETH + 7,423.73 USDC ≈ $16.7k**
  — matches the known position value independently; delta has risen from
  3.42 toward the 9.01 all-WETH max exactly as the monotonic curve predicts

## Public interface

### `dexpaprika.lp.clmath` (reference doc formulas, Decimal prec=60)

- `tick_to_sqrt_price(tick)`, `sqrt_price_from_x96(x96)`
- `position_amounts(liquidity, tick_lower, tick_upper, sqrt_price) ->
  (amount0_raw, amount1_raw)` — three regimes (below/in/above range)
- `price_from_tick(tick, dec0=18, dec1=6)` — verified: −200975 → $1,871.56

### `dexpaprika.lp.discovery`

- Constants: `SICKLE_FACTORY`, `VOTER` (Base); NFPM registry from
  `Settings.base_nfpm_addresses` (default: canonical + second deployment);
  gauge path over `Settings.base_tracked_pools` (default: Richard's pool).
- `discover(rpc, wallet, *, settings, block) -> list[LpPosition]`:
  1. Sickle lookup; `owner()` mismatch → sickle EXCLUDED with a warning
     field (custody verification, §0.1 standing lesson).
  2. Per NFPM × owner: `balanceOf` → `tokenOfOwnerByIndex` → `positions`;
     keep liquidity > 0.
  3. Gauge path per tracked pool: `Voter.gauges` → `stakedValues` for each
     owner → positions.
  4. Pool via `factory()` + `getPool`; `slot0` at the same block; amounts +
     price via clmath; `in_range` flag.
  All reads at the caller's pinned block.
- `record(conn, wallet, position, ts)` — `positions` upsert (venue
  `aerodrome-slipstream`, chain base, kind lp, group lp_hedge, external_id
  `nfpm:tokenId`) + `position_events` observed row with full state.

### CLI

```
dexpaprika lp snapshot [--address 0x...] [--record] --json
```

Wallets default to ALL included EVM registry wallets. Pin once per run;
`--record` also writes a `snapshots` row (kind `lp`). (SECTION_PLAN's
`snapshot --kind lp` arrives in S6 as the orchestrator; `lp snapshot` is
the underlying command — noted deviation.)

## Error cases

- No sickle (zero address) → wallet-only enumeration, no error.
- Sickle owner mismatch → excluded + warning surfaced in output.
- Zero positions → clean empty list (valid state).
- Unknown pool (getPool → 0x0) → position recorded WITHOUT amounts, flagged
  `pool_unresolved` — never silently valued wrong.

## Standards obligations

- All reads at one pinned block (§2); Decimal-only math (prec=60 context —
  reference doc: float error at 1e-6 on sqrtPriceX96 moves USD by dollars).
- Fixtures = live probe raw calls (§5); amounts asserted against the
  probe's independently computed values.
- Coverage ≥90% on clmath + discovery.

## References read (Step 2)

concentrated-liquidity-math--summary.md (ALL formulas implemented from it);
aerodrome-slipstream--integration-guide--gauges.md (gauge recipe + Voter
address + depositor caveat); aerodrome--summary--quick-reference.md (Voter
address confirmed); REFERENCE_INDEX §0.1(a)/(d) re-read (Krystal can't be
sole source; custody-not-ownership).
