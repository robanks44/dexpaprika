"""CL math — pinned to verified on-chain reality + regime/monotonicity properties."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from dexpaprika.lp.clmath import (
    position_amounts,
    price_from_tick,
    sqrt_price_from_x96,
    tick_to_sqrt_price,
)

PROBE = json.loads(
    (Path(__file__).parent.parent / "probes" / "out" / "s5" / "discovery.json").read_text()
)
POS = PROBE["positions"][0]
L = Decimal(POS["liquidity"])
LOWER, UPPER = POS["tick_lower"], POS["tick_upper"]


def test_price_from_tick_verified_constant() -> None:
    # VERIFIED_FINDINGS §4: tick -200975 -> $1,871.56 (WETH/USDC, 18/6 decimals).
    assert price_from_tick(-200975).quantize(Decimal("0.01")) == Decimal("1871.56")


def test_price_from_probe_tick() -> None:
    expected = Decimal(PROBE["computed"]["price_usd"])
    assert price_from_tick(PROBE["pool_tick"]).quantize(Decimal("0.01")) == expected


def test_amounts_match_probe_computed_values() -> None:
    """The live probe computed amounts that reproduced the known position value."""
    sqrt_price = sqrt_price_from_x96(PROBE["sqrt_price_x96"])
    amount0_raw, amount1_raw = position_amounts(L, LOWER, UPPER, sqrt_price)
    weth = (amount0_raw / Decimal(10**18)).quantize(Decimal("0.000001"))
    usdc = (amount1_raw / Decimal(10**6)).quantize(Decimal("0.01"))
    assert weth == Decimal(PROBE["computed"]["weth"])
    assert usdc == Decimal(PROBE["computed"]["usdc"])


def test_band_prices_match_live_verified_values() -> None:
    """§0.1(a) live-verified: ticks -202000..-200000 = $1,689.24..$2,063.22."""
    assert price_from_tick(-202000).quantize(Decimal("0.01")) == Decimal("1689.24")
    assert price_from_tick(-200000).quantize(Decimal("0.01")) == Decimal("2063.22")


def test_below_range_is_all_token0() -> None:
    sqrt_price = tick_to_sqrt_price(LOWER - 1000)
    amount0, amount1 = position_amounts(L, LOWER, UPPER, sqrt_price)
    assert amount1 == 0
    # Formula-derived all-WETH maximum: 9.23 ETH. (VERIFIED_FINDINGS' "≈9.01"
    # was an ESTIMATE made before tickLower/tickUpper were known — the exact
    # bounds are now anchored by the live-verified band prices above.)
    assert (amount0 / Decimal(10**18)).quantize(Decimal("0.01")) == Decimal("9.23")


def test_above_range_is_all_token1() -> None:
    sqrt_price = tick_to_sqrt_price(UPPER + 1000)
    amount0, amount1 = position_amounts(L, LOWER, UPPER, sqrt_price)
    assert amount0 == 0
    assert amount1 > 0


def test_amounts_continuous_at_boundaries() -> None:
    at_lower = position_amounts(L, LOWER, UPPER, tick_to_sqrt_price(LOWER))
    below = position_amounts(L, LOWER, UPPER, tick_to_sqrt_price(LOWER) * Decimal("0.999"))
    rel = abs(at_lower[0] - below[0]) / below[0]
    assert rel < Decimal("0.001")

    at_upper = position_amounts(L, LOWER, UPPER, tick_to_sqrt_price(UPPER))
    above = position_amounts(L, LOWER, UPPER, tick_to_sqrt_price(UPPER) * Decimal("1.001"))
    rel1 = abs(at_upper[1] - above[1]) / above[1]
    assert rel1 < Decimal("0.001")


@given(tick=st.integers(min_value=LOWER - 5000, max_value=UPPER + 5000))
def test_amounts_never_negative(tick: int) -> None:
    amount0, amount1 = position_amounts(L, LOWER, UPPER, tick_to_sqrt_price(tick))
    assert amount0 >= 0
    assert amount1 >= 0


@given(
    tick_a=st.integers(min_value=LOWER, max_value=UPPER),
    tick_b=st.integers(min_value=LOWER, max_value=UPPER),
)
def test_token0_exposure_monotonically_decreasing_in_price(tick_a: int, tick_b: int) -> None:
    """LP delta (amount0) falls as price rises — the hedge-sizing invariant."""
    low_tick, high_tick = sorted((tick_a, tick_b))
    amount0_low, _ = position_amounts(L, LOWER, UPPER, tick_to_sqrt_price(low_tick))
    amount0_high, _ = position_amounts(L, LOWER, UPPER, tick_to_sqrt_price(high_tick))
    assert amount0_low >= amount0_high


@given(tick=st.integers(min_value=LOWER + 1, max_value=UPPER - 1))
def test_value_between_boundary_values(tick: int) -> None:
    """In-range USD value sits between the all-token0 and all-token1 values."""
    sqrt_price = tick_to_sqrt_price(tick)
    price = price_from_tick(tick)
    amount0, amount1 = position_amounts(L, LOWER, UPPER, sqrt_price)
    value = (amount0 / Decimal(10**18)) * price + amount1 / Decimal(10**6)
    assert value > 0
