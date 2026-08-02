"""Wallet holdings (holdings group): native balance + per-chain token registry."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, localcontext

from pydantic import BaseModel

from dexpaprika.chains.abi import selector
from dexpaprika.chains.rpc import EvmRpcClient

# Per-chain token registry: symbol -> (address, decimals). Config-extendable later.
TOKENS: dict[str, dict[str, tuple[str, int]]] = {
    "base": {
        "WETH": ("0x4200000000000000000000000000000000000006", 18),
        "USDC": ("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 6),
        "AERO": ("0x940181a94A35A4569E4529A3CDfB74e38FD98631", 18),
    },
    "arbitrum": {
        "WETH": ("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", 18),
        "USDC": ("0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6),
    },
}


class Holding(BaseModel):
    chain: str
    symbol: str
    token: str | None  # None = native
    amount: Decimal
    block_number: int


def _amount(raw_int: int, decimals: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal(raw_int) / Decimal(10) ** decimals


def read_holdings(rpc: EvmRpcClient, chain: str, wallet: str, *, block: int) -> list[Holding]:
    """Native + registry-token balances at the pinned block; zero balances skipped."""
    holdings: list[Holding] = []
    native_raw = rpc.rpc("eth_getBalance", [wallet, hex(block)])
    native = _amount(int(str(native_raw), 16), 18)
    if native > 0:
        holdings.append(
            Holding(chain=chain, symbol="ETH", token=None, amount=native, block_number=block)
        )
    for symbol, (token, decimals) in TOKENS.get(chain, {}).items():
        raw = rpc.call(token, selector("balanceOf(address)") + f"{int(wallet, 16):064x}", block)
        amount = _amount(int(raw, 16), decimals)
        if amount > 0:
            holdings.append(
                Holding(chain=chain, symbol=symbol, token=token, amount=amount, block_number=block)
            )
    return holdings


def record(
    conn: sqlite3.Connection, wallet: str, chain: str, holdings: list[Holding], ts: str
) -> int:
    for holding in holdings:
        external_id = f"{chain}:{holding.symbol}"
        conn.execute(
            "INSERT INTO positions (wallet_ref, venue, chain, kind, external_id, group_tag,"
            " opened_at) VALUES (?, 'wallet', ?, 'holding', ?, 'holdings', ?)"
            " ON CONFLICT(wallet_ref, venue, chain, kind, external_id) DO NOTHING",
            (wallet, chain, external_id, ts),
        )
        position_id = conn.execute(
            "SELECT id FROM positions WHERE wallet_ref=? AND venue='wallet' AND chain=?"
            " AND kind='holding' AND external_id=?",
            (wallet, chain, external_id),
        ).fetchone()["id"]
        state = holding.model_dump(mode="json")
        state["source"] = "on-chain:balance"
        conn.execute(
            "INSERT INTO position_events (position_id, ts, type, delta_json, state_json)"
            " VALUES (?, ?, 'observed', '{}', ?)",
            (position_id, ts, json.dumps(state)),
        )
    return len(holdings)
