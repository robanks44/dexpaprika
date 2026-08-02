# S4 spec — GMX data client

## Purpose

Typed client for the GMX v2 REST API (positions incl. related orders,
orders, markets) with peer failover, an exact Decimal scaling layer
(a scaling bug = silent wrong alerts), defensive parsing ("Expanding" API),
and DB recording of the hedge leg.

## Probe evidence (Step 2b — 2026-08-02, `probes/out/s4/`)

- Both peers live and byte-identical for positions (`gmxapi.io` ≡ `gmxapi.ai`).
- Position payload: numerics arrive as JSON STRINGS (parse via
  `Decimal(str)`, never float); carries `indexName`, `isLong`, `poolName`,
  `entryPrice`, `markPrice`, `liquidationPrice`, `leverage`, `pnl`, pending
  fee fields, `relatedOrders`.
- Scalings re-verified live: 1e30 USD fields (entry 1869.0949…, liq
  2793.18…), 1e4 leverage (1.9694), token decimals (WETH 18 / USDC 6),
  **triggerPrice/1e12 = 1925.0 exactly**, SL `sizeDeltaUsd == uint256max`
  (full close), `autoCancel: true`, orderType 6.
- **Probe catch:** `/markets` does NOT carry `indexName` (docs assumption) —
  rows are `{symbol: "ETH/USD [WETH-USDC]", marketTokenAddress,
  indexTokenAddress, long/shortTokenAddress, leverageTiers, …}`. Filtering
  by indexName works on POSITIONS only. Index-token decimals for trigger
  scaling resolve via markets: marketAddress → indexTokenAddress → decimals
  registry.
- Standalone `/orders` live (1 order = the SL).

## Public interface — `dexpaprika.clients.gmx`

- `GmxClient(conn, *, settings, clients=None, sleeper, clock)` — one
  `HttpTransport` per peer from `settings.gmx_rest_peers`; requests try
  peers in order, failing over on any TransportError (incl. open circuit);
  both dead → error naming both peers. Provider `gmx` (S2.5 seed).
- Scaling layer (exact Decimal, parse from strings):
  - `scale_usd(raw) = Decimal(raw) / 10**30`
  - `scale_factor(raw) = Decimal(raw) / 10**4` (leverage, pnl %)
  - `scale_tokens(raw, decimals)` — token native decimals from a registry
    keyed by token address (WETH 18, USDC 6, WBTC 8, …); unknown address →
    actionable error, never a guess.
  - `scale_trigger(raw, index_decimals) = Decimal(raw) / 10**(30-index_decimals)`
  - `uint256max` sizeDeltaUsd → `is_full_close=True`, no fake number.
- Models: `GmxPosition` (scaled fields + `related_orders` + raw),
  `GmxOrder` (`order_type` int + `order_kind` name, 6=StopLossDecrease;
  scaled trigger; full-close flag), `GmxMarket`.
- `get_positions(address, include_orders=True)` — empty `[]` is a VALID
  "no open positions" (HTTP 200), returned as `[]` — a closed/liquidated
  position looks exactly like this (alert logic in S7/S8 keys off it).
- `get_markets()`; `hedge_positions(address)` — `indexName=="ETH/USD" and
  not isLong` filter on positions.
- `record_observation(position)` — upsert `positions` row (venue `gmx`,
  chain `arbitrum`, kind `perp`, group `lp_hedge`, external_id = position
  key), append `position_events` type `observed` with the scaled state,
  upsert `orders` rows for related orders. Idempotent re-runs.

### CLI

```
dexpaprika gmx positions [--address 0x...] [--record] --json
```

`--address` defaults to the single INCLUDED EVM wallet in the registry
(error if none or ambiguous — agent gets told to pass --address).

## Error cases

- Both peers failing → TransportError naming both. Unknown token address in
  scaling → error telling operator to extend the registry (never silent).
- Unknown/new orderType → parsed with `order_kind="unknown-<n>"`, never a
  crash (defensive: API labeled "Expanding").
- Missing optional fields → None, not KeyError.

## Standards obligations

- Money math: Decimal from strings end-to-end; property tests on the
  scaling table; fixtures are the live probe dumps (§5).
- Quota-gated via shared transport (120/min conservative seed).
- Coverage ≥90% on the client + scaling layer.

## References read (Step 2)

VERIFIED_FINDINGS §2/§2.1/§2.2 (endpoints, scaling table, rejected
alternatives — do not re-litigate subgraph/Reader); REFERENCE_INDEX §1;
dex-docs QUICK-REFERENCE GMX section; live probes above (which corrected
the /markets shape).
