# SICKLE-CUSTODY-FINDINGS.md — LP custody blocker RESOLVED (handoff to build loop)

**Session:** 2026-08-04 Cowork chat with Richard (strategy review)
**Author:** Claude (probe-verified live on Base mainnet during the session)
**Status of claims:** every on-chain value below was returned by a live `eth_call` / `eth_getCode` on 2026-08-04 via `mainnet.base.org` (fallback `base-rpc.publicnode.com`). Nothing is inferred from docs alone.

---

## 1. What changed

The open blocker in REFERENCE_INDEX §0.1(a) / VERIFIED_FINDINGS §4 ("LP NFT custodian is an unidentified EIP-1167 proxy; delta-matched rebalance strategy blocked on live tick reads") is **resolved**.

**Richard confirmed he staked the LP via vfat.io.** The unknown custodian `0x6c1b2006…` is his personal vfat **Sickle** — a per-user smart-contract wallet (DSProxy-style) that vfat's SickleFactory deploys once per user per chain, as an EIP-1167 minimal proxy. It holds the LP NFT directly. The full permissionless read path (enumerate → ticks/liquidity) was verified end-to-end this session.

Consequence: **the 2026-08-03 decision-log strategy (delta-matched rebalance bands, no SL) is no longer data-blocked.** S5's "first job" (identify the custodian) is done; what remains for S5 is implementing the recipe below.

## 2. Probe evidence (2026-08-04, Base mainnet)

### 2a. Proxy identity

- `eth_getCode(0x6c1b20062970c886082687d8121d06aaace8886e)` →
  `0x363d3d373d3d3d363d73fff75d099baee29f447866bc5299cd67c04761c85af43d82803e903d91602b57fd5bf3`
  (canonical EIP-1167; embedded implementation = `0xfff75d099baee29f447866bc5299cd67c04761c8`, which has ~3KB of code)
- `Sickle.owner()` (selector `0x8da5cb5b`) → `0xC155a616E39d7b83E37e8fd9d2106E1BC056d7Fe` = **Richard's wallet** (matches VERIFIED_FINDINGS §1). This is the identity confirmation.
- `approved()` and `factory()` selectors revert on the proxy (tested; not part of its ABI).

### 2b. NFT custody + enumeration (on the SECOND SlipStream NFPM `0xa990c6a764b73bf43cee5bb40339c3322fb9d55f`)

| Call | Result |
|---|---|
| `ownerOf(5056427)` (`0x6352211e`) | `0x6c1b2006…` — the Sickle holds the NFT **directly** (NOT staked into a CLGauge) |
| `balanceOf(sickle)` (`0x70a08231`) | 1 |
| `tokenOfOwnerByIndex(sickle, 0)` (`0x2f745c59`) | 5056427 |

### 2c. Live position read — `positions(5056427)` (`0x99fbab88`)

| Field | Value |
|---|---|
| token0 / token1 | WETH `0x4200…0006` / USDC `0x8335…2913` |
| tickSpacing | 500 |
| tickLower / tickUpper | **−202000 / −200000** |
| liquidity | **3987414535131380** |
| Implied range (`1.0001^tick × 1e12`) | **$1,689.24 – $2,063.22** |

Identical to the 2026-08-01 probe values in REFERENCE_INDEX §0.1(a) — position unchanged, recipe reproduces known-good data.

## 3. The read recipe for S5 (LP discovery + delta inputs)

1. **Sickle discovery:** preferred = query SickleFactory for wallet→sickle (factory address NOT yet captured — see §5 gaps). Interim = configured sickle address `0x6c1b2006…` with a mandatory sanity gate: `Sickle.owner() == Richard's wallet`, else fail loudly.
2. **Enumerate:** `balanceOf(sickle)` + `tokenOfOwnerByIndex(sickle, i)` on **every known SlipStream NFPM deployment** — at least two exist on Base (`0xa990…` live one, canonical `0x8279…`). Never hardcode one NFPM.
3. **Read:** `positions(tokenId)` → tickLower/tickUpper/liquidity. SlipStream tuple layout: nonce, operator, token0, token1, tickSpacing, tickLower, tickUpper, liquidity, … (int24 fields need sign-extension).
4. Combine with pool `slot0()` tick (VERIFIED_FINDINGS §4) → current LP composition/delta per `concentrated-liquidity-math--summary.md`.

**Design cautions (must survive into the code):**

- **Re-ranging mints a new tokenId.** Never pin #5056427; re-enumerate every cycle. `balanceOf(sickle) == 0` is a signal to widen the search, not "no position" (standing lesson: verify custody, not ownership).
- **Gauge fallback:** vfat's NftFarmStrategy CAN stake NFTs into gauges. Today the Sickle holds the NFT directly, but if enumeration ever comes up empty, check gauge custody paths (including the CLGauge recipe doc) before concluding the position is gone.
- **Execution note:** managing/withdrawing the LP goes through vfat strategy contracts (NftFarmStrategy `depositErc721`/`withdrawErc721`, SweepStrategy) as Sickle owner — the EOA cannot `safeTransferFrom` the NFT out of the NFPM directly. Reads are permissionless; no key material involved.

## 4. Library update (already done — do not redo)

- New doc saved this session: `C:\Users\NoBloat\COWORK\CONTEXT\_inbox\vfat-sickle--integration-guide.md` (Source: docs.vfat.io/sickle + github.com/vfat-io/sickle-public + these probes; Captured 2026-08-04). The context-library-loop will file it; until then read it from `_inbox`.
- Library previously had **no vfat/Sickle coverage** (checked reference\INDEX.md 2026-08-04).

## 5. Suggested doc edits for the loop (NOT yet applied — loop's call per its own rules)

1. **PROGRESS.md decision log:** add entry "2026-08-04 — LP custody blocker resolved: custodian is Richard's vfat Sickle; delta-matched rebalance strategy unblocked" citing this file; also add a Research-findings-log row.
2. **REFERENCE_INDEX §0.1(a):** append a dated correction note: proxy identified (vfat Sickle, owner-verified), read recipe in this file + the `_inbox` library doc; keep the original text for history.
3. **VERIFIED_FINDINGS §4:** update the "UNRESOLVED: LP range bounds" item per its own convention (strike through + date, never delete): custodian = vfat Sickle; enumeration + `positions()` recipe verified 2026-08-04.
4. **REFERENCE_INDEX §2:** the gauges doc's "custody-resolver" framing is now known to be the wrong path for THIS position (still a valid fallback path — see §3 caution 2).

## 6. Still open (unchanged by this session)

- **SickleFactory address on Base** not captured — needed to replace the hardcoded sickle address in discovery. Source candidates: github.com/vfat-io/sickle-public, BaseScan verification of `0xfff75d09…`'s deployer chain.
- Whether NFPM `0xa990…` is vfat-specific or a general Aerodrome redeploy — unknown.
- **S9 live-execute rehearsal** (testnet + mainnet SL-nudge) — scripts ready, not yet run (status per Richard pending).
- **Rebalance-band design** (band width, resize triggers, cost-per-rebalance vs. historical SL premiums) — now the front strategy thread; not yet designed.
- Interim SL-widening patch (2026-08-03 strategy-doc rule) remains in force until rebalance bands ship.
