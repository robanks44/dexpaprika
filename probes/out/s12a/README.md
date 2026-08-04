# S12a probe gate — full-variable capture evidence (2026-08-04)

Purpose: ground the S12a full-variable test fixture in real raw shapes and pin the
exact gaps between what the recording seam emits today and the acceptance list.

## LP — `lp_live_2026-08-04.json` (REAL live dump)

Live `dexpaprika lp snapshot` against Base RPC, block ~49,525,5xx, wallet
`0xC155…d7Fe`. Position 5056427 (WETH/USDC, custody=sickle, in_range=true).

Acceptance coverage of the recorded `LpPosition.state_json`:

| Acceptance field | Emitted today? | Field |
|---|---|---|
| pool price + tick | ✅ | `sqrt_price_x96`, `pool_tick` |
| in/out-of-range | ✅ | `in_range` |
| pool liquidity | ✅ | `liquidity` |
| position token amounts | ✅ | `amount0`, `amount1` |
| unclaimed fees (raw) | ✅ | `tokens_owed0`, `tokens_owed1` |
| token price (token0) | ⚠ partial | `price_usd` (token0-in-USDC only) |
| **both token prices** | ❌ ADD | → `token0_price_usd`, `token1_price_usd` |
| **pool volume** | ❌ ADD | off-chain (DexPaprika pool metrics) → `pool_volume_usd_24h` |

Two LP additions for S12a (Richard approved 2026-08-04):
- `token0_price_usd` + `token1_price_usd` — explicit both-token prices. token1 (USDC)
  priced via stable numeraire; warning recorded if token decimals unknown.
- `pool_volume_usd_24h` — sourced from the DexPaprika pool-metrics read already made in
  the same cycle; **null-with-reason** when DexPaprika has no row for the pool (never
  fabricated). Volume is NOT on-chain-derivable from slot0.

## Hedge — `hedge_schema_2026-08-04.json` (SCHEMA-DERIVED)

Live GMX REST peers returned HTTP 400 on `/positions` this session (endpoint-side).
Hedge fixture is therefore a mocked `GmxClient` (standards: zero-network tests). The
recorded `GmxPosition.state_json` already covers every acceptance field EXCEPT SL size.

One hedge addition for S12a:
- `stop_loss_orders` — list of `{trigger, size_usd, is_full_close}` so SL **size** lives
  in the hedge `state_json` (today only the SL trigger price list is in state; SL size is
  in the `orders` table). Trigger list `stop_loss_triggers` retained for back-compat.
