# REFERENCE_INDEX.md — Read-Before-Code Map

**Rule (hard gate):** Before designing or coding any section, look up that section's topic
below and READ the mapped reference documents first. Log which docs were read in
PROGRESS.md under the section entry. If a mapped doc turns out to be irrelevant, note that
too — the point is that the check always happens.

These folders live on Richard's machine and must be connected to (or copied into) the
dexpaprika project session. **Canonical source (repointed 2026-08-01):** the
loop-maintained CONTEXT library — connect `C:\Users\NoBloat\COWORK\CONTEXT`:

- `reference\` → `C:\Users\NoBloat\COWORK\CONTEXT\reference` — filed technical docs,
  renamed to `subject--type.md`; its `INDEX.md` is the authoritative lookup
- library root → `C:\Users\NoBloat\COWORK\CONTEXT` — docs not yet filed by the
  context-library-loop keep their original names here until filed
- `APIDOCS\` → `C:\Users\NoBloat\COWORK\CONTEXT\APIDOCS`
- `encylopedia Uig\` → `C:\Users\NoBloat\COWORK\CONTEXT\encylopedia Uig`

(The old `PROJECTS\UIG\Context Docs|APIDOCS|encylopedia Uig` mirrors still exist but are
NOT loop-maintained and will drift — prefer the CONTEXT library.)

`APIDOCS\INDEX.md` is a full index of the web3-ethereum-defi (eth_defi) library docs —
consult it whenever the topic maps to on-chain reads/writes in Python.

**Path note (2026-08-01):** library organization is COMPLETE — no docs remain at the
library root. Everything technical lives in `reference\` (see its `INDEX.md`), personal
docs in `personal\`. If a path here is ever missing, check `reference\INDEX.md` first.

---

## 0. Session-verified findings (READ FIRST, every section — added 2026-08-01)

- `..\dexpaprika-context-docs\VERIFIED_FINDINGS.md` — facts verified live on 2026-07-31
  (in the DexPaprika API project folder). **Outranks every doc below when they disagree.**
  Contains: Richard's actual positions + scope decisions (answers most SETUP Step 1
  questions); GMX REST API endpoints/peers with the critical scaling table (1e30 / 1e4 /
  **1e12 for order triggerPrice** — a silent-wrong-alert trap); rejected GMX alternatives
  (subgraph, Reader contract, wrong Arbiscan address); DexPaprika ~2% price skew vs
  on-chain (range logic must read the pool contract); CoinStats/CoinGecko connector
  caveats; hedge delta math.

### 0.1 ⚠ CORRECTIONS from the 2026-08-01 late session (probe-verified, outrank §0 above)

Four findings from live probes against Richard's own wallet. Each one would have cost a
build section if discovered during implementation instead of now. Raw evidence:
`Defi_Tracker_3.0\probes\` (scripts + `out\` JSON dumps).

**(a) The LP custody question is NOT resolved by the CLGauge recipe.** SETUP_PROMPT and
§2 below previously stated the blocker was closed via
`Voter.gauges(pool) → stakedValues(depositor) → NFPM.positions(tokenId)`. **That recipe
returns nothing for Richard's actual position.** Verified on Base:

```
Live position  : Aerodrome CL NFT #5056427
Position mgr   : 0xa990c6a764b73bf43cee5bb40339c3322fb9d55f
                 ("Slipstream Position NFT v1", symbol AERO-CL-POS)
                 -- a SECOND Slipstream deployment, NOT the canonical
                 0x827922686190790b37229fd06084350E74485b72
Custodian      : 0x6c1b20062970c886082687d8121d06aaace8886e
                 -- a 45-byte EIP-1167 minimal-proxy clone.
                 NOT the wallet. NOT a CLGauge: stakedValues(), pool(),
                 voter() and rewardToken() ALL revert on it.
Pair/ticks     : WETH/USDC, tickSpacing 500, ticks -202000..-200000
Price range    : $1,689.24 .. $2,063.22 per WETH  (pool ~$1,869 -> IN RANGE)
Liquidity      : 3987414535131380  (non-zero = live)
```

Consequences for the build: (1) **there is more than one Aerodrome Slipstream deployment
on Base** — never hardcode a single NFPM address; (2) the gauge recipe is a valid path but
NOT the only one, and it is not the one that applies here; (3) `NFPM.balanceOf(wallet)`
returns **0** for this position, so **any enumeration built on "NFTs the wallet owns" will
find nothing.** Identify `0x6c1b2006…`'s implementation (it is a proxy — read its
implementation slot) before designing LP discovery. **This is still an open design
question; treat it as the first thing S5 must resolve, and probe before coding.**

**(b) Zerion requires the undocumented `sync=true` or it serves silently stale data.**
Same endpoint, same wallet, seconds apart: cached returned **2 positions / $16,155**;
`sync=true` returned **8 positions / $49,844** — adding the entire Base Aave position and
the live Aerodrome LP. Nothing in the cached response signals staleness (every row carried
a fresh `updated_at`). `sync` is documented **only in a marketing FAQ blog**, absent from
the API reference and the OpenAPI spec. Docs say it "can take up to 30 seconds."
Full detail + the complete 58-path field schema: `reference\zerion--api-reference.md`.
**Generalize this:** treat cache-freshness as a correctness gate on every provider
(ENGINEERING_STANDARDS §2), not a performance detail.

**(c) Zerion `pool_address` is the position-manager address, not the pool.** Joining on it
is wrong — `Voter.gauges(pool_address)` returns the zero address as a direct result. Also:
`no_filter` **double-counts** (aTokens in `only_simple` + the same collateral as a position
in `only_complex`: $24,384 + $49,844 summed), and variable-debt tokens price at **$0.00**
so debt silently vanishes from any `only_simple` total. Pick one filter mode; never sum.

**(d) Krystal has a real coverage gap.** It returned only 3 CLOSED positions, all from the
canonical `0x8279…` manager, and **missed the live position on `0xa990…` entirely.** It
remains the best single source for CL LP breadth (Aerodrome CL on Base, Uniswap v3/v4,
HyperSwap on HyperEVM — 13 chains) and it does return price range, pending fees, farm
rewards and an APR split — but it **cannot be the sole source of truth for LP.**
Cross-check against on-chain reads. See `reference\defi-position-aggregation--playbook.md`
(its "Option B" is weakened by this finding and should be re-read with (a) and (d) in mind).

**Standing lesson for every section: "the wallet does not hold it" is not evidence a
position does not exist.** Verify custody, not ownership. Two independent lookups
(`NFPM.balanceOf` and Alchemy `getNFTsForOwner`) both correctly returned zero for a live
$16.7k position. Probe before you design, and prefer a five-minute API call over an
assumption — that is what turned all four of these up.

Also in the project folder (complement the UIG mirrors):
- `..\dex-docs\` — curated DexPaprika reference (GMX sections corrected 2026-08-01)
- `..\dexpaprika-context-docs\llm.md` — full official DexPaprika docs dump (grep, don't read)

---

## 1. GMX short hedge (positions, funding, orders, risk)

The core of this project. Read before ANY hedge-related section.

- `APIDOCS\gmx-docs\` — full GMX docs mirror. Key files:
  - `api/gmx-api/introduction.md`, `get-positions-info.md`, `get-markets-info.md`,
    `get-rates.md`, `get-ohlcv.md`, `get-orders-by-address.md` — REST data API
  - `trading/order-types.md`, `trading/fees.md`, `trading/liquidations.md` — mechanics that
    hedge math MUST model (funding, borrow fees, liquidation price)
  - `api/contracts/` — only if on-chain interaction (order creation) is in scope
  - `sdk/v2.md` — official SDK option for reads/writes
- `APIDOCS\api\gmx.md` — eth_defi GMX integration (Python)
- `APIDOCS\tutorials\gmx-v2-price-analysis.md`, `gmx-swap.md`, `gmx-ccxt-freqtrade.md`
- `reference\gmx-python-sdk--api-reference.md` — NEW (2026-08-01): Python write-path
  (DecreaseOrder, debug_mode dry-run) — ONLY if executor capability is approved
- `reference\deribit--api-reference--eth-options.md` — NEW: put-option hedge
  alternative (no stop-out failure mode) — read if the strategy section evaluates puts

## 2. LP positions & downside risk (what the hedge protects)

- `encylopedia Uig\` — **school material; this is strategy ground truth.** The hedge
  engine's risk rules must be reconciled with these before that section is coded:
  - `4_Liquidity providers.pdf` and modules `4.1.pdf` … `4.9.pdf` (LP curriculum)
  - `5.1.pdf` … `5.6.pdf`, `5.action items.pdf` (follow-on modules / actions)
  - `43_criteria.xlsx` (criteria checklist — likely pool/position selection rules)
  - `Top Dex's and Tools.pdf`
- `reference\orca-whirlpool--integration-guide.md`, `reference\raydium-clmm--integration-guide.md`,
  `reference\kamino--integration-guide.md`, `reference\kamino--api-reference--trimmed.md` (Solana CLMM)
- `reference\aerodrome--integration-guide.md`,
  `reference\aerodrome--summary--quick-reference.md` (Base)
- `reference\krystal--integration-guide.md`, `reference\krystal--summary--implementation.md`,
  `reference\krystal--api-reference--client.md`, `reference\krystal--api-reference--gitbook.md`
  (multi-chain LP position aggregation; gitbook dump is 211KB — grep, don't read)
- `reference\thegraph-uniswap-subgraph--integration-guide.md`; `APIDOCS\api\uniswap_v3.md` and
  `APIDOCS\tutorials\uniswap-v3-liquidity-analysis.md` (concentrated-liquidity math)
- `reference\aerodrome-slipstream--integration-guide--gauges.md` — CLGauge custody recipe:
  Voter.gauges(pool) → stakedValues(depositor) → NFPM.positions(tokenId). **⚠ This is ONE
  custody path, not THE answer — see §0.1(a): Richard's live position #5056427 is held by an
  EIP-1167 proxy on a second Slipstream deployment and this recipe returns nothing for it.**
- `reference\concentrated-liquidity-math--summary.md` — NEW: tick/amount/delta/IL
  formulas the hedge-sizing sections implement (MUST read before delta math)
- `reference\web3py--api-reference.md` + `reference\rpc-providers--api-reference--base-arbitrum.md`
  — NEW: on-chain read patterns + RPC failover matrix (read before any RPC section)

## 3. Market data (DexPaprika + backups)

- **DexPaprika** — local docs DO exist now: `..\dex-docs\` (curated, verified) and
  `..\dexpaprika-context-docs\llm.md` (full dump). Verify against
  https://docs.dexpaprika.com at build time — base URL `https://api.dexpaprika.com/`; public, no key
  required; free tier ≈200k req/mo (500k with free key) at 30 req/min — the client MUST
  rate-limit accordingly. Endpoints: networks, dexes, pools, tokens, OHLCV, transactions.
- `reference\coingecko--api-reference.md` — reference prices
- `reference\dexscreener--api-reference.md` (merged: main endpoints + rate limits +
  chart/OHLCV — replaces `dexscreener.md` + `dexscreener_charts.md`);
  `reference\dexscreener-timescaledb--integration-guide.md` (pipeline into TimescaleDB)
- `reference\moralis--api-reference.md` (+ `reference\moralis--summary--quick-reference.md`);
  `reference\etherscan--api-reference.md`,
  `reference\etherscan-v2--api-reference--multichain.md`
- `APIDOCS\bitquery-docs\` (large GraphQL mirror — use its `INDEX.md`);
  `reference\graphql--api-reference.md`
- `reference\zerion--api-reference.md` — Zerion API v1. **MUST pass `sync=true` or you get
  silently stale data (§0.1b); `pool_address` is the position manager, not the pool (§0.1c)**
- `reference\geckoterminal--api-reference.md` — NEW (2026-08-01): the documented OHLCV
  source (DexScreener's chart endpoint is NOT in official docs — do not build on it)
- `reference\coinstats--api-reference.md` — NEW: direct-call reference + credit
  economics (400/call DeFi) and the wrong-poolAddress caveat, session-verified
### §3b. Multi-provider quota tracking (design, self-contained)

ENGINEERING_STANDARDS §2 requires this; the design is recorded HERE so it does not depend
on a parked file. Originally Richard's `MULTI_API_TRACKING.md` (2025-09), now in
`CONTEXT\_to_delete\` pending his review — a copy also sits in
`Defi_Tracker_3.0\context\MULTI_API_TRACKING.md`. Restore either if the full text is wanted.

Per-provider `APIConfig`: `base_url`, `rate_limit` + `rate_period` (second|minute|hour|day),
`has_credits`, `credit_limit`, `cost_per_endpoint` (credit weight per endpoint), `free_tier`,
and per-tier limits. Backed by two tables: a universal **API call log** (timestamp, provider,
endpoint, credits consumed, status, latency) and an **API configuration** table — so quota
consumption is queryable and adding a provider is config, not code.

Known quota shapes to seed it with:

| Provider | Limit | Credit model |
|---|---|---|
| DexPaprika | 30 req/min; 200k/mo (500k with free key) | flat — 1 request = 1 credit, unweighted |
| Krystal | 50k units free, then paid tiers | weighted — ~10 units/call; positions cost more |
| CoinStats | — | weighted — **400 credits per DeFi call** |
| Etherscan | 5 req/s free, 20 standard, 50 advanced | flat, tier-dependent |
| Zerion | 10 RPS, ~2k calls/mo free | flat — but see §0.1(b): `sync=true` is mandatory |
| Hyperliquid | 1200 weight/min per IP | weighted — `clearinghouseState` = weight 2 |

The tracker MUST support unweighted providers as a first-class case, not a special case.
Enforce limits per **upstream**, not per client instance — several chain tasks share one
Etherscan key and one price oracle.


## 4. Portfolio analysis & recording

- `reference\defi-portfolio--best-practices.md` (copy also at
  `..\dexpaprika-context-docs\`). Scope note (Richard-confirmed 2026-08-01): its §4
  multi-chain SDK aggregation, §9 distribution tiers, and all Redis patterns are OUT of
  scope — single-user CLI; in-process token bucket + SQLite suffice. Its §3 event-based
  position lifecycle and §8 SQLite WAL/DECIMAL guidance ARE adopted.
- `reference\defi-tax-tracking--best-practices.md` (record-keeping requirements)
- `reference\solana--integration-guide--lp-tracker.md`,
  `reference\solana--summary--quick-reference.md`; `reference\walletconnect--integration-guide.md`
- `reference\tao-bittensor--integration-guide.md`, `reference\bitcoin--integration-guide.md`,
  `reference\aave-v2--integration-guide.md`, `reference\aave-v3--integration-guide.md`
  (only if those assets are in the portfolio scope)
- `reference\defi-position-aggregation--playbook.md` — NEW (2026-08-01): source map,
  architecture options & test plan for aggregating positions (lend/borrow, perp hedges,
  CL LP, looping) — read alongside VERIFIED_FINDINGS.md before any aggregation design

## 5. Storage layer

- `reference\sqlite--best-practices.md` — primary DB reference
- `reference\timescaledb--api-reference--lp-tracker.md`,
  `reference\timescaledb--best-practices--style-guide.md`
  (cloud-migration path for time-series data)

## 5b. Runtime: alerts, scheduling, testing (added 2026-08-01)

- `reference\ntfy--api-reference.md` — publish API, priorities, ACTION BUTTONS for the
  approval-gated executor, poll-based approval pattern (read before any alert section)
- `reference\python-scheduling--playbook--windows.md` — Task Scheduler task definitions
  (catch-up, no-overlap, hang guard) + APScheduler knobs (read before the scheduler section)
- `reference\pytest--best-practices.md` — gate-suite patterns: zero-network markers,
  frozen fixtures of VERIFIED_FINDINGS payloads, Decimal assertions (read at Section 1)

## 6. Service/API layer & project structure

- `reference\python--best-practices--project-structure.md` — repo layout standard
- `reference\flask--best-practices--production.md` (+ `reference\flask--dataset--docs.json`)
  — only if an HTTP layer is chosen; the system is CLI-first (see ENGINEERING_STANDARDS.md)
- `reference\python-peps--summary--trimmed.md`; `_to_delete\CONTEXT7_BEST_PRACTICES.md`
  (parked in quarantine pending Richard's review)

## 7. Security & secrets

- `_to_delete\SECURITY_POLICY.md`, `_to_delete\SECURITY_ENHANCEMENT_COMPLETE.md` — prior
  project's security baseline; this project must meet or exceed it. **Both currently
  parked in CONTEXT\_to_delete pending Richard's review — restore if still the baseline**
  (copies may exist in PROJECTS\UIG\Context Docs)
- `reference\python-keyring--setup--windows.md` — OS-keyring secret storage pattern
- `reference\microsoft-security-cryptography--api-reference.txt` (Windows crypto reference)
- `personal\insurance-policy--strategy.md` (strategy-level risk doc — delta-neutral LP
  hedging; now in the library's personal\ collection)

---

*Freshness rule: local mirrors can drift. For anything involving live endpoints or SDK
versions (DexPaprika, GMX API, gmx-python-sdk, eth_defi), verify against current online
docs (Context7 or WebFetch) at coding time and note discrepancies in PROGRESS.md.*
