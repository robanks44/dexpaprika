"""Hedge coverage engine — the Insurance Policy strategy as code (S7).

Ground truth: ``personal\\insurance-policy--strategy.md`` (school material),
reconciled in docs/specs/S7-hedge-engine.md. Read-only: this module
analyzes and recommends; it never places orders (S9, separately gated).

Key rules implemented verbatim:
- Quadrants: the price range split into 4 equal quarters; Q1 top, Q4
  bottom; Q3 is the profit zone; the Q4 boundary is the decision point.
- Break-even sizing: S* = (V(entry) - V(floor)) / (entry - floor) — short
  PnL at the floor exactly offsets the LP's value loss (property-tested).
- Coverage: notional % (doc metric) + ETH-terms delta ratio.
- SL placement rule: near the Q1/Q2 boundary; deviations flagged.
- Correlated failure (VERIFIED_FINDINGS §6): SL below the ceiling means a
  stop-out and a top-exit are the same rally — always flagged.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

from pydantic import BaseModel

from dexpaprika.config import Settings
from dexpaprika.lp.clmath import position_amounts, price_from_tick, tick_to_sqrt_price

_PREC = 50
_TOKEN0_DECIMALS = 18
_TOKEN1_DECIMALS = 6


class LpParams(BaseModel):
    tick_lower: int
    tick_upper: int
    liquidity: int


class ShortParams(BaseModel):
    size_eth: Decimal
    entry_price: Decimal
    sl_trigger: Decimal | None = None
    collateral_usd: Decimal | None = None


class SimPoint(BaseModel):
    price_usd: Decimal
    quadrant: str
    lp_value_usd: Decimal
    lp_change_usd: Decimal
    short_pnl_usd: Decimal
    net_usd: Decimal


class HedgeAnalysis(BaseModel):
    price_usd: Decimal
    quadrant: str
    range_position_pct: Decimal | None
    floor_price: Decimal
    ceiling_price: Decimal
    q4_profit_take_price: Decimal
    q1_q2_boundary_price: Decimal
    lp_delta_eth: Decimal
    lp_delta_max_eth: Decimal
    lp_value_usd: Decimal
    short_size_eth: Decimal
    coverage_ratio_eth: Decimal | None
    coverage_notional_pct: Decimal | None
    delta_matched_target_eth: Decimal
    break_even_short_size: Decimal | None
    rebalance_needed: bool
    distance_to_floor_pct: Decimal
    distance_to_ceiling_pct: Decimal
    distance_to_sl_pct: Decimal | None
    premium_if_sl_fires: Decimal | None
    flags: list[str]


def _sqrt_price_for(price_usd: Decimal) -> Decimal:
    """Raw sqrt price from a decimal-adjusted USD price (token0/token1 18/6)."""
    with localcontext() as ctx:
        ctx.prec = _PREC
        raw_price = price_usd / Decimal(10) ** (_TOKEN0_DECIMALS - _TOKEN1_DECIMALS)
        return raw_price.sqrt()


def _lp_state(lp: LpParams, price_usd: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """(delta_eth, usdc_amount, value_usd) at ``price_usd``."""
    with localcontext() as ctx:
        ctx.prec = _PREC
        amount0_raw, amount1_raw = position_amounts(
            Decimal(lp.liquidity), lp.tick_lower, lp.tick_upper, _sqrt_price_for(price_usd)
        )
        eth = amount0_raw / Decimal(10) ** _TOKEN0_DECIMALS
        usdc = amount1_raw / Decimal(10) ** _TOKEN1_DECIMALS
        return eth, usdc, eth * price_usd + usdc


def _quadrant(price_usd: Decimal, floor: Decimal, ceiling: Decimal) -> tuple[str, Decimal | None]:
    if price_usd < floor:
        return "below-range", None
    if price_usd >= ceiling:
        return "above-range", None
    with localcontext() as ctx:
        ctx.prec = _PREC
        pct = (price_usd - floor) / (ceiling - floor)
    if pct < Decimal("0.25"):
        return "Q4", pct
    if pct < Decimal("0.50"):
        return "Q3", pct
    if pct < Decimal("0.75"):
        return "Q2", pct
    return "Q1", pct


def analyze(
    lp: LpParams,
    short: ShortParams | None,
    price_usd: Decimal,
    *,
    settings: Settings,
) -> HedgeAnalysis:
    with localcontext() as ctx:
        ctx.prec = _PREC
        floor = price_from_tick(lp.tick_lower)
        ceiling = price_from_tick(lp.tick_upper)
        quadrant, pct = _quadrant(price_usd, floor, ceiling)
        delta_eth, _usdc, lp_value = _lp_state(lp, price_usd)
        floor_sqrt = tick_to_sqrt_price(lp.tick_lower)
        max_raw, _ = position_amounts(
            Decimal(lp.liquidity), lp.tick_lower, lp.tick_upper, floor_sqrt * Decimal("0.999")
        )
        delta_max = max_raw / Decimal(10) ** _TOKEN0_DECIMALS

        size = short.size_eth if short else Decimal(0)
        flags: list[str] = []

        coverage_eth: Decimal | None = None
        coverage_notional: Decimal | None = None
        if short is None or size == 0:
            flags.append("naked-lp")
        elif delta_eth == 0:
            flags.append("over-hedged")  # any short against zero delta = pure exposure
        else:
            coverage_eth = size / delta_eth
            coverage_notional = (size * price_usd) / (delta_eth * price_usd) * Decimal(100)

        band = Decimal(str(settings_band(settings)))
        target = delta_eth
        rebalance_needed = False
        if delta_max > 0:
            deviation = abs(size - target) / delta_max
            rebalance_needed = deviation > band
        if coverage_eth is not None:
            if size - target > band * delta_max:
                flags.append("over-hedged")
            elif target - size > band * delta_max:
                flags.append("under-hedged")

        # Break-even sizing at the SHORT's entry (or current price when naked).
        entry = short.entry_price if short else price_usd
        _e_eth, _e_usdc, value_at_entry = _lp_state(lp, entry)
        _f_eth, _f_usdc, value_at_floor = _lp_state(lp, floor)
        break_even: Decimal | None = None
        if entry > floor:
            break_even = (value_at_entry - value_at_floor) / (entry - floor)

        distance_floor = (price_usd - floor) / price_usd * Decimal(100)
        distance_ceiling = (ceiling - price_usd) / price_usd * Decimal(100)
        if min(abs(distance_floor), abs(distance_ceiling)) <= 2:
            flags.append("near-band-edge")

        q1_q2 = floor + Decimal("0.75") * (ceiling - floor)
        q4_take = floor + Decimal("0.25") * (ceiling - floor)

        distance_sl: Decimal | None = None
        premium: Decimal | None = None
        if short is not None and short.sl_trigger is not None:
            distance_sl = (short.sl_trigger - price_usd) / price_usd * Decimal(100)
            premium = size * (short.entry_price - short.sl_trigger)
            if abs(distance_sl) <= 3:
                flags.append("price-near-sl")
            if short.sl_trigger < ceiling:
                flags.append("sl-correlated-with-top-exit")
            if short.sl_trigger < q1_q2:
                flags.append("sl-below-q1q2-rule")

        max_position = settings.max_position_usd
        if max_position > 0 and target * price_usd > max_position:
            flags.append("target-exceeds-configured-max")

        return HedgeAnalysis(
            price_usd=price_usd,
            quadrant=quadrant,
            range_position_pct=(pct * 100).quantize(Decimal("0.01")) if pct is not None else None,
            floor_price=floor,
            ceiling_price=ceiling,
            q4_profit_take_price=q4_take,
            q1_q2_boundary_price=q1_q2,
            lp_delta_eth=delta_eth,
            lp_delta_max_eth=delta_max,
            lp_value_usd=lp_value,
            short_size_eth=size,
            coverage_ratio_eth=coverage_eth,
            coverage_notional_pct=coverage_notional,
            delta_matched_target_eth=target,
            break_even_short_size=break_even,
            rebalance_needed=rebalance_needed,
            distance_to_floor_pct=distance_floor,
            distance_to_ceiling_pct=distance_ceiling,
            distance_to_sl_pct=distance_sl,
            premium_if_sl_fires=premium,
            flags=flags,
        )


def settings_band(settings: Settings) -> Decimal:
    return settings.hedge_rebalance_band


def simulate(
    lp: LpParams,
    short: ShortParams | None,
    prices: list[Decimal],
    *,
    entry_price: Decimal,
) -> list[SimPoint]:
    """Dual-curve P&L (doc metric 9): LP value vs linear short PnL per price."""
    with localcontext() as ctx:
        ctx.prec = _PREC
        floor = price_from_tick(lp.tick_lower)
        ceiling = price_from_tick(lp.tick_upper)
        _entry_eth, _entry_usdc, entry_value = _lp_state(lp, entry_price)
        points: list[SimPoint] = []
        for price in prices:
            _eth, _usdc, value = _lp_state(lp, price)
            lp_change = value - entry_value
            short_pnl = (
                short.size_eth * (short.entry_price - price) if short is not None else Decimal(0)
            )
            quadrant, _pct = _quadrant(price, floor, ceiling)
            points.append(
                SimPoint(
                    price_usd=price,
                    quadrant=quadrant,
                    lp_value_usd=value,
                    lp_change_usd=lp_change,
                    short_pnl_usd=short_pnl,
                    net_usd=lp_change + short_pnl,
                )
            )
        return points
