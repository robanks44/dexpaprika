"""Concentrated-liquidity math (reference: concentrated-liquidity-math--summary.md).

All arithmetic in Decimal at explicit precision 60 — the reference doc's
warning is literal: float error at 1e-6 on sqrtPriceX96 moves USD amounts by
dollars. Formulas are the Uniswap v3 whitepaper set; constants cross-checked
against verified on-chain reads (tick -200975 -> $1,871.56).
"""

from __future__ import annotations

from decimal import Decimal, localcontext

_PRECISION = 60
_TICK_BASE = Decimal("1.0001")
_Q96 = Decimal(2) ** 96


def tick_to_sqrt_price(tick: int) -> Decimal:
    """sqrt(1.0001^tick) — raw (non-decimal-adjusted) sqrt price."""
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return _TICK_BASE ** (Decimal(tick) / 2)


def sqrt_price_from_x96(sqrt_price_x96: int) -> Decimal:
    """slot0's sqrtPriceX96 → raw sqrt price."""
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return Decimal(sqrt_price_x96) / _Q96


def price_from_tick(tick: int, dec0: int = 18, dec1: int = 6) -> Decimal:
    """Decimal-adjusted price of token0 in token1 units (WETH/USDC → USD)."""
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return _TICK_BASE ** Decimal(tick) * Decimal(10) ** (dec0 - dec1)


def position_amounts(
    liquidity: Decimal, tick_lower: int, tick_upper: int, sqrt_price: Decimal
) -> tuple[Decimal, Decimal]:
    """Raw token amounts (base units) for a position at ``sqrt_price``.

    Three regimes (below / in / above range); amount0 (token0 exposure) is
    monotonically decreasing in price — the hedge-sizing invariant.
    """
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        sqrt_lower = _TICK_BASE ** (Decimal(tick_lower) / 2)
        sqrt_upper = _TICK_BASE ** (Decimal(tick_upper) / 2)
        if sqrt_price <= sqrt_lower:  # below range: all token0
            amount0 = liquidity * (sqrt_upper - sqrt_lower) / (sqrt_lower * sqrt_upper)
            return amount0, Decimal(0)
        if sqrt_price >= sqrt_upper:  # above range: all token1
            return Decimal(0), liquidity * (sqrt_upper - sqrt_lower)
        amount0 = liquidity * (sqrt_upper - sqrt_price) / (sqrt_price * sqrt_upper)
        amount1 = liquidity * (sqrt_price - sqrt_lower)
        return amount0, amount1
