"""Aave v3 account reads (defi group) — on-chain, probe-verified (S6).

`getUserAccountData` scalings: base-currency values 1e8 (USD), health
factor 1e18, thresholds in bps. Aave is recorded for portfolio analysis
only — it is OUT of hedge scope (Richard-confirmed).
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, localcontext

from pydantic import BaseModel

from dexpaprika.chains.abi import selector, sign_extend
from dexpaprika.chains.rpc import EvmRpcClient
from dexpaprika.config import Settings

VENUE = "aave-v3"


class AaveAccount(BaseModel):
    wallet: str
    total_collateral_usd: Decimal
    total_debt_usd: Decimal
    available_borrows_usd: Decimal
    liq_threshold_bps: int
    ltv_bps: int
    health_factor: Decimal
    block_number: int


def _scaled(word_hex: str, places: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 50
        return Decimal(sign_extend(int(word_hex, 16), 256)) / Decimal(10) ** places


def read_account(rpc: EvmRpcClient, wallet: str, *, settings: Settings, block: int) -> AaveAccount:
    data = selector("getUserAccountData(address)") + f"{int(wallet, 16):064x}"
    raw = rpc.call(settings.aave_pool_base, data, block)
    words = [raw[2:][i : i + 64] for i in range(0, len(raw) - 2, 64)]
    return AaveAccount(
        wallet=wallet,
        total_collateral_usd=_scaled(words[0], 8),
        total_debt_usd=_scaled(words[1], 8),
        available_borrows_usd=_scaled(words[2], 8),
        liq_threshold_bps=int(words[3], 16),
        ltv_bps=int(words[4], 16),
        health_factor=_scaled(words[5], 18),
        block_number=block,
    )


def record(conn: sqlite3.Connection, wallet: str, account: AaveAccount, ts: str) -> int:
    """Two defi rows — lend (collateral) and borrow (debt) — with HF in state."""
    recorded = 0
    for kind, amount in (
        ("lend", account.total_collateral_usd),
        ("borrow", account.total_debt_usd),
    ):
        conn.execute(
            "INSERT INTO positions (wallet_ref, venue, chain, kind, external_id, group_tag,"
            " opened_at) VALUES (?, ?, 'base', ?, 'account', 'defi', ?)"
            " ON CONFLICT(wallet_ref, venue, chain, kind, external_id) DO NOTHING",
            (wallet, VENUE, kind, ts),
        )
        position_id = conn.execute(
            "SELECT id FROM positions WHERE wallet_ref=? AND venue=? AND chain='base'"
            " AND kind=? AND external_id='account'",
            (wallet, VENUE, kind),
        ).fetchone()["id"]
        state = account.model_dump(mode="json")
        state["amount_usd"] = str(amount)
        state["source"] = "on-chain:aave-pool"
        conn.execute(
            "INSERT INTO position_events (position_id, ts, type, delta_json, state_json)"
            " VALUES (?, ?, 'observed', '{}', ?)",
            (position_id, ts, json.dumps(state)),
        )
        recorded += 1
    return recorded
