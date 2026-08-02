# S3 spec — DexPaprika client

## Purpose

Typed client for the DexPaprika REST API (market history/volume recording)
behind the S2.5 quota tracker at 30 req/min, with Decimal-safe parsing,
retry/backoff, a per-upstream circuit breaker, and DB recording carrying
`source` + `as_of` on every datapoint.

**Role boundary (VERIFIED_FINDINGS §3):** DexPaprika prices are indexed
aggregates with verified ~2% skew vs on-chain — they are for HISTORY and
VOLUME. Range/edge logic reads the pool contract (S4.5/S5). This client
exposes no "current price for hedge math" API; module docs state it.

## Probe evidence (Step 2b — 2026-08-02, committed `probes/out/s3/`)

- `networks.json` — 200; `base`/`arbitrum` present.
- `pool_details.json` — 200; keys incl. `last_price_usd`, `liquidity_usd`,
  window objects `5m/15m/30m/1h/6h/24h` (`volume_usd`, `txns`, buys/sells),
  `token_reserves`, `price_stats`; **`fee` STILL null** for SlipStream.
- `ohlcv_24h.json` — 200; `GET .../ohlcv?start=YYYY-MM-DD&interval=24h&limit=N`
  → array of `{time_open, time_close, open, high, low, close, volume}`;
  prices are JSON FLOATS → transport must parse with `parse_float=Decimal`.
- `transactions.json` — 200; swap rows with amounts + prices.

## Public interface

### `dexpaprika.clients.base` (shared by S4+ clients)

- `HttpTransport(base_url, provider, conn, *, client=None, sleeper, clock)`
  — httpx wrapper enforcing: HTTPS-only base URL, 30 s timeout, 10 MB
  response cap, JSON parsed with `parse_float=Decimal` (money never through
  float), quota `wait_for_slot` → request → `record` (status, latency,
  correlation id), tenacity retry (429/5xx/transport errors, exp backoff +
  jitter, bounded attempts), and a circuit breaker per upstream (N=5
  consecutive failures → open for cooldown 60 s → half-open probe).
  `TransportError` carries the fix-oriented message.
- Injectable `httpx.Client` (tests use respx/`httpx.MockTransport`),
  injectable clock/sleeper (offline deterministic).

### `dexpaprika.clients.dexpaprika`

- `DexPaprikaClient(settings, conn, transport=None)` — provider name
  `dexpaprika` (seeded S2.5).
  - `get_networks() -> list[Network]`
  - `get_pool(network, address) -> PoolDetails` — pydantic: Decimal prices,
    `fee: Decimal | None` (SlipStream: expect None — fee tier comes
    on-chain), 24h window model, raw payload retained.
  - `get_ohlcv(network, address, *, start, interval="24h", limit) ->
    list[Candle]` — Decimal OHLC; interval validated against the documented
    set (1m…24h).
  - `record_pool_metrics(pool) -> int` / `record_ohlcv(network, address,
    candles) -> int` — persist with `source='dexpaprika'`, `as_of` = fetch
    time (UTC ISO); OHLCV upserts on (network, pool, interval, ts_start) —
    idempotent re-runs don't duplicate.

### Migration `0002_market_data.sql`

- `pool_metrics(id, ts, network, pool_address, source, price_usd TEXT,
  liquidity_usd TEXT, volume_24h_usd TEXT, txns_24h INTEGER, fee TEXT,
  raw_json)` + index (network, pool_address, ts DESC).
- `ohlcv(id, network, pool_address, interval, ts_start, ts_end, open TEXT,
  high TEXT, low TEXT, close TEXT, volume TEXT, source, as_of,
  UNIQUE(network, pool_address, interval, ts_start))`.

### CLI

```
dexpaprika market pool  --network base --address 0x... [--record] --json
dexpaprika market ohlcv --network base --address 0x... --start YYYY-MM-DD
                        [--interval 24h] [--limit N] [--record] --json
```

Exit 1 + `{"error": ...}` on quota/circuit/HTTP failures; `--record`
requires a migrated DB (actionable error otherwise).

## Error cases

- 429/5xx → retry with backoff; still failing → TransportError (breaker
  counts it). 404 → no retry, clear "not found" error. Response >10 MB →
  refused. Non-JSON → error naming the endpoint. Circuit open → immediate
  error telling operator to wait. Credit budget exhausted → QuotaExceededError
  surfaces (no silent spend).

## Standards obligations

- Client-side 30 rpm via quota tracker (§2); every call logged.
- Decimal end-to-end; `parse_float=Decimal` at the transport (§1).
- pydantic models at the boundary; raw dicts never leave the client (§1).
- New deps: httpx (+ respx dev) — own `chore(deps)` commit.
- Coverage ≥90% on transport + client.

## References read (Step 2)

`dex-docs/QUICK-REFERENCE.md` (endpoints incl. OHLCV params + intervals,
rate limits 200k/mo + 30/min, error codes — verified against live probe);
REFERENCE_INDEX §3 + §0 (skew rule); live docs base URL confirmed at setup.
