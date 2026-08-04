"""Custody-aware LP discovery (ARCHITECTURE §5.1, probe-verified recipe).

Standing lesson (§0.1): "the wallet does not hold it" is not evidence a
position does not exist — verify CUSTODY. Candidate owners are the wallet
plus its vfat.io Sickle (owner()-verified); enumeration runs across a
config-driven NFPM registry (≥2 SlipStream deployments exist on Base);
the CLGauge staked path covers tracked pools. Pools resolve generally via
``NFPM.factory()`` + ``factory.getPool(token0, token1, tickSpacing)`` —
never a hardcoded pool address.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from dexpaprika.chains.abi import encode_uint, selector, sign_extend
from dexpaprika.chains.rpc import ChainRpcError, EvmRpcClient
from dexpaprika.config import Settings
from dexpaprika.lp.clmath import position_amounts, price_from_tick, sqrt_price_from_x96

# Base-chain constants (probe-verified 2026-08-02).
SICKLE_FACTORY = "0x71D234A3e1dfC161cc1d081E6496e76627baAc31"
VOTER = "0x16613524e02ad97eDfeF371bC883F2F5d6C480A5"

VENUE = "aerodrome-slipstream"
_ZERO = "0x" + "0" * 40

# Token decimals for amount scaling (Base). Unknown pairs record raw only.
_TOKEN_DECIMALS = {
    "0x4200000000000000000000000000000000000006": 18,  # WETH
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 6,  # USDC
}

# USD-pegged tokens used as the pricing numeraire (Base). token_price_usd is
# only derivable when exactly one side is a known stable; otherwise it is
# recorded null-with-reason (never fabricated — ENGINEERING_STANDARDS §2).
_USD_STABLES = frozenset(
    {
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC (native)
    }
)


def _addr_arg(address: str) -> str:
    return f"{int(address, 16):064x}"


def _addr_from(word_hex: str) -> str:
    return "0x" + word_hex[-40:]


class LpPosition(BaseModel):
    nfpm: str
    token_id: int
    custody: Literal["wallet", "sickle", "gauge"]
    custodian: str
    token0: str
    token1: str
    tick_spacing: int
    tick_lower: int
    tick_upper: int
    liquidity: int
    tokens_owed0: int
    tokens_owed1: int
    pool: str | None = None
    pool_unresolved: bool = False
    pool_tick: int | None = None
    sqrt_price_x96: int | None = None
    amount0: Decimal | None = None  # decimal-adjusted token0 (e.g. WETH)
    amount1: Decimal | None = None  # decimal-adjusted token1 (e.g. USDC)
    price_usd: Decimal | None = None  # token0 priced in token1 units (back-compat)
    token0_price_usd: Decimal | None = None  # explicit both-token USD prices (S12a)
    token1_price_usd: Decimal | None = None
    pool_volume_usd_24h: Decimal | None = None  # off-chain (DexPaprika); set by recorder
    in_range: bool | None = None
    block_number: int
    warnings: list[str] = []


def _positions_words(rpc: EvmRpcClient, nfpm: str, token_id: int, block: int) -> list[str]:
    raw = rpc.call(nfpm, selector("positions(uint256)") + encode_uint(token_id), block)
    return [raw[2:][i : i + 64] for i in range(0, len(raw) - 2, 64)]


def _sint(word_hex: str) -> int:
    return sign_extend(int(word_hex, 16), 256)


def _enumerate_owner(
    rpc: EvmRpcClient, nfpm: str, owner: str, block: int
) -> list[tuple[int, list[str]]]:
    balance = int(rpc.call(nfpm, selector("balanceOf(address)") + _addr_arg(owner), block), 16)
    found: list[tuple[int, list[str]]] = []
    for index in range(balance):
        token_raw = rpc.call(
            nfpm,
            selector("tokenOfOwnerByIndex(address,uint256)")
            + _addr_arg(owner)
            + encode_uint(index),
            block,
        )
        token_id = int(token_raw, 16)
        found.append((token_id, _positions_words(rpc, nfpm, token_id, block)))
    return found


def _staked_token_ids(rpc: EvmRpcClient, gauge: str, owner: str, block: int) -> list[int]:
    try:
        raw = rpc.call(gauge, selector("stakedValues(address)") + _addr_arg(owner), block)
    except ChainRpcError:
        return []
    blob = bytes.fromhex(raw[2:]) if len(raw) > 2 else b""
    if len(blob) < 64:
        return []
    count = int.from_bytes(blob[32:64], "big")
    return [
        int.from_bytes(blob[64 + 32 * i : 96 + 32 * i], "big")
        for i in range(count)
        if len(blob) >= 96 + 32 * i
    ]


def discover(rpc: EvmRpcClient, wallet: str, *, settings: Settings, block: int) -> list[LpPosition]:
    """All CL LP positions for ``wallet`` at the pinned ``block``."""
    warnings: list[str] = []

    # 1. Candidate owners: wallet + owner()-verified Sickle.
    owners: list[tuple[str, str]] = [("wallet", wallet)]
    sickle_raw = rpc.call(SICKLE_FACTORY, selector("sickles(address)") + _addr_arg(wallet), block)
    sickle = _addr_from(sickle_raw)
    if int(sickle, 16) != 0:
        sickle_owner = _addr_from(rpc.call(sickle, selector("owner()"), block))
        if sickle_owner.lower() == wallet.lower():
            owners.append(("sickle", sickle))
        else:
            warnings.append(f"sickle {sickle} EXCLUDED: owner() is {sickle_owner}, not the wallet")

    nfpms = list(settings.base_nfpm_addresses)
    factory_cache: dict[str, str] = {}
    pool_state_cache: dict[str, tuple[int, int]] = {}
    positions: list[LpPosition] = []
    seen: set[tuple[str, int]] = set()

    def add_position(nfpm: str, token_id: int, custody: str, custodian: str) -> None:
        if (nfpm.lower(), token_id) in seen:
            return
        words = _positions_words(rpc, nfpm, token_id, block)
        if len(words) < 12:  # token unknown to this NFPM (short/empty return)
            return
        liquidity = int(words[7], 16)
        if liquidity == 0:
            return
        seen.add((nfpm.lower(), token_id))
        token0 = _addr_from(words[2])
        token1 = _addr_from(words[3])
        tick_spacing = int(words[4], 16)
        position = LpPosition(
            nfpm=nfpm,
            token_id=token_id,
            custody=custody,  # type: ignore[arg-type]  # literal narrowed by callers
            custodian=custodian,
            token0=token0,
            token1=token1,
            tick_spacing=tick_spacing,
            tick_lower=_sint(words[5]),
            tick_upper=_sint(words[6]),
            liquidity=liquidity,
            tokens_owed0=int(words[10], 16),
            tokens_owed1=int(words[11], 16),
            block_number=block,
            warnings=list(warnings),
        )
        _resolve_pool_and_value(position)
        positions.append(position)

    def _resolve_pool_and_value(position: LpPosition) -> None:
        nfpm_key = position.nfpm.lower()
        if nfpm_key not in factory_cache:
            factory_cache[nfpm_key] = _addr_from(
                rpc.call(position.nfpm, selector("factory()"), block)
            )
        factory = factory_cache[nfpm_key]
        pool_raw = rpc.call(
            factory,
            selector("getPool(address,address,int24)")
            + _addr_arg(position.token0)
            + _addr_arg(position.token1)
            + encode_uint(position.tick_spacing),
            block,
        )
        pool = _addr_from(pool_raw)
        if int(pool, 16) == 0:
            position.pool_unresolved = True
            position.warnings.append("pool unresolved via factory.getPool — amounts omitted")
            return
        position.pool = pool
        if pool.lower() not in pool_state_cache:
            slot0 = rpc.call(pool, selector("slot0()"), block)
            sqrt_price_x96 = int(slot0[2:66], 16)
            tick = _sint(slot0[66:130])
            pool_state_cache[pool.lower()] = (sqrt_price_x96, tick)
        sqrt_price_x96, tick = pool_state_cache[pool.lower()]
        position.sqrt_price_x96 = sqrt_price_x96
        position.pool_tick = tick
        position.in_range = position.tick_lower <= tick < position.tick_upper
        dec0 = _TOKEN_DECIMALS.get(position.token0.lower())
        dec1 = _TOKEN_DECIMALS.get(position.token1.lower())
        if dec0 is None or dec1 is None:
            position.warnings.append("unknown token decimals — raw liquidity recorded only")
            return
        amount0_raw, amount1_raw = position_amounts(
            Decimal(position.liquidity),
            position.tick_lower,
            position.tick_upper,
            sqrt_price_from_x96(sqrt_price_x96),
        )
        position.amount0 = amount0_raw / Decimal(10) ** dec0
        position.amount1 = amount1_raw / Decimal(10) ** dec1
        position.price_usd = price_from_tick(tick, dec0, dec1)
        _set_usd_prices(position)

    def _set_usd_prices(position: LpPosition) -> None:
        """Both-token USD prices from the tick price, using a stable numeraire.

        price_usd is token0-in-token1. Derivable to USD only when exactly one
        side is a known USD stable; a non-stable pair records null-with-reason.
        """
        token0_stable = position.token0.lower() in _USD_STABLES
        token1_stable = position.token1.lower() in _USD_STABLES
        if token1_stable and not token0_stable and position.price_usd is not None:
            position.token0_price_usd = position.price_usd
            position.token1_price_usd = Decimal(1)
        elif token0_stable and not token1_stable and position.price_usd not in (None, 0):
            position.token0_price_usd = Decimal(1)
            position.token1_price_usd = Decimal(1) / position.price_usd
        else:
            position.warnings.append(
                "no single USD-stable in pair — token USD prices not derivable"
            )

    # 2. NFPM enumeration per owner.
    for nfpm in nfpms:
        for custody, owner in owners:
            for token_id, _words in _enumerate_owner(rpc, nfpm, owner, block):
                add_position(nfpm, token_id, custody, owner)

    # 3. Gauge path over tracked pools.
    for pool in settings.base_tracked_pools:
        gauge = _addr_from(rpc.call(VOTER, selector("gauges(address)") + _addr_arg(pool), block))
        if int(gauge, 16) == 0:
            continue
        for _custody, owner in owners:
            for token_id in _staked_token_ids(rpc, gauge, owner, block):
                for nfpm in nfpms:
                    try:
                        add_position(nfpm, token_id, "gauge", gauge)
                        break
                    except ChainRpcError:
                        continue

    return positions


def record(conn: sqlite3.Connection, wallet: str, position: LpPosition, ts: str) -> None:
    """Upsert the position row + append an observed event with full state."""
    external_id = f"{position.nfpm.lower()}:{position.token_id}"
    conn.execute(
        "INSERT INTO positions (wallet_ref, venue, chain, kind, external_id, group_tag,"
        " opened_at, metadata_json) VALUES (?, ?, 'base', 'lp', ?, 'lp_hedge', ?, ?)"
        " ON CONFLICT(wallet_ref, venue, chain, kind, external_id) DO UPDATE SET"
        " metadata_json=excluded.metadata_json",
        (
            wallet,
            VENUE,
            external_id,
            ts,
            json.dumps({"pool": position.pool, "custody": position.custody}),
        ),
    )
    position_id = conn.execute(
        "SELECT id FROM positions WHERE wallet_ref=? AND venue=? AND chain='base'"
        " AND kind='lp' AND external_id=?",
        (wallet, VENUE, external_id),
    ).fetchone()["id"]
    state = position.model_dump(mode="json")
    conn.execute(
        "INSERT INTO position_events (position_id, ts, type, delta_json, state_json)"
        " VALUES (?, ?, 'observed', '{}', ?)",
        (position_id, ts, json.dumps(state)),
    )
