# S7 spec — Hedge coverage engine (school-material reconciled)

## Purpose

The analytical core: given the LP position, the GMX short, and the pool
price, compute coverage, quadrant position, break-even sizing, distances,
flags, and what-if simulations. Read-only — recommendations, never orders.

## School-material reconciliation (the hard gate for this section)

Ground truth: `personal\insurance-policy--strategy.md` (the distilled
delta-neutral LP hedging strategy). Adopted VERBATIM into the engine:

- **Quadrant model:** range split into 4 equal price quadrants; Q1 top,
  Q4 bottom. Q3 = profit zone (short gains faster than LP loses); Q4
  boundary = decision point (close both for profit vs ride to floor).
- **Break-even sizing rule:** short sized so short PnL at the range floor
  equals the LP's value loss from entry to floor:
  `S* = (V(p_entry) − V(floor)) / (p_entry − floor)` — NOT total LP value.
- **Coverage % (doc metric 2):** short notional / LP volatile-asset
  exposure — reported alongside the ETH-terms ratio (short_eth /
  lp_delta_eth) used for delta-matching.
- **SL placement rule:** near the Q1/Q2 boundary (75% mark). The engine
  reports the SL's quadrant position and flags deviation. (Richard's live
  SL $1,925 sits at ~63% — inside Q2, below the rule's boundary.)
- **Premium/re-arm economics:** cumulative premium tracking and the 30%-of-
  fees insurance budget are S8+ ledger features; the engine exposes
  `premium_if_sl_fires` (the cost of a stop-out at current sizing) so the
  ledger can accrue it.
- **Correlated-failure flag (VERIFIED_FINDINGS §6):** "LP exits top of
  range" and "SL fires" are the same ETH rally — flagged whenever the SL
  trigger sits below the upper bound (stop-out before/at top-exit).
- Checked `encylopedia Uig\5.action items.pdf`: Quest-5 fundamental-
  analysis checklist (macro pillars, FDV, adoption metrics) — strategy
  CONTEXT, not hedge mechanics; noted per the reference-gate rule.
  Deeper module-4 reconciliation items (range-width selection, pool
  criteria) belong to strategy sections, not this engine.

## Probe gate

N/A — the engine computes from recorded/verified states (no external
source). Fixture anchors: the live S5 LP state (ticks −202000/−200000,
L 3987414535131380, tick −201114) + live S4 short (7.038573 ETH, entry
1869.09…, SL 1925, collateral $6,579.73).

## Public interface — `dexpaprika.hedge.engine`

- `LpParams(tick_lower, tick_upper, liquidity)`,
  `ShortParams(size_eth, entry_price, sl_trigger, collateral_usd)`
- `analyze(lp, short, price_usd, *, settings) -> HedgeAnalysis`:
  lp_delta_eth, lp_delta_max (floor), lp_value_usd, short_size_eth,
  coverage_ratio_eth, coverage_notional_pct, quadrant (Q1..Q4 /
  below-range / above-range), range position pct, band floor/ceiling +
  Q4 profit-take + Q1/Q2 boundary prices, distances (to floor/ceiling/SL,
  %), break_even_short_size (S*), delta_matched_target (= lp_delta_eth),
  rebalance_needed (|short − target| / lp_delta_max > band, config
  `hedge_rebalance_band` default 0.075), premium_if_sl_fires
  (= size·(sl − entry), negative = cost), flags list:
  `naked-lp` (no short), `over-hedged`/`under-hedged` (beyond band),
  `sl-correlated-with-top-exit`, `sl-below-q1q2-rule`,
  `near-band-edge` (≤ 2% from either bound), `price-near-sl` (≤ 3%),
  `target-exceeds-configured-max` (vs S9 hard limits when set).
- `simulate(lp, short, prices) -> list[SimPoint]`: per price — LP value,
  LP loss vs entry-value, short PnL, net, quadrant (the doc's dual-curve
  P&L, metric 9).
- Break-even property (tested): with `short.size = S*`, net at the floor
  is ~0 (|net| < $0.01 on the live fixture).

### CLI

```
dexpaprika hedge status --json               # from latest recorded states
dexpaprika hedge simulate --price P [...] --json
                        [--short-size E] [--curve N]  # N points floor→ceiling
```

`hedge status` requires recorded lp+hedge snapshots (actionable error
otherwise); GMX `[]` (closed short) analyzes as `naked-lp` — that state is
exactly what S8 must alert on.

## Standards obligations

- Decimal end-to-end; property tests: coverage never negative, break-even
  invariant, quadrant totality, delta-match monotonicity; ≥90% coverage.
- Read-only; `simulate` and (future) `execute` remain separate commands.
