"""Hedge engine — school-material rules on the live fixture + invariants."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dexpaprika.config import Settings
from dexpaprika.hedge.engine import (
    LpParams,
    ShortParams,
    analyze,
    simulate,
)
from dexpaprika.lp.clmath import price_from_tick

# Live fixture: S5 LP state + S4 short state (probe/verifier-recorded).
LP = LpParams(tick_lower=-202000, tick_upper=-200000, liquidity=3987414535131380)
SHORT = ShortParams(
    size_eth=Decimal("7.038573460810147061"),
    entry_price=Decimal("1869.094972567349999993975015634280"),
    sl_trigger=Decimal("1925"),
    collateral_usd=Decimal("6579.725157"),
)
LIVE_PRICE = Decimal("1845.72")
FLOOR = price_from_tick(-202000)
CEILING = price_from_tick(-200000)


def settings() -> Settings:
    return Settings.load()


class TestQuadrants:
    @pytest.mark.parametrize(
        ("pct_of_range", "expected"),
        [
            (Decimal("0.10"), "Q4"),
            (Decimal("0.30"), "Q3"),
            (Decimal("0.60"), "Q2"),
            (Decimal("0.90"), "Q1"),
        ],
    )
    def test_quadrant_mapping(self, pct_of_range: Decimal, expected: str) -> None:
        price = FLOOR + pct_of_range * (CEILING - FLOOR)
        result = analyze(LP, SHORT, price, settings=settings())
        assert result.quadrant == expected

    def test_out_of_range_labels(self) -> None:
        below = analyze(LP, SHORT, FLOOR - 50, settings=settings())
        above = analyze(LP, SHORT, CEILING + 50, settings=settings())
        assert below.quadrant == "below-range"
        assert above.quadrant == "above-range"

    def test_live_price_is_q3_profit_zone(self) -> None:
        """(1845.72 - 1689.24) / (2063.22 - 1689.24) = ~41.8% -> Q3."""
        result = analyze(LP, SHORT, LIVE_PRICE, settings=settings())
        assert result.quadrant == "Q3"


class TestCoverage:
    def test_live_fixture_over_hedged(self) -> None:
        result = analyze(LP, SHORT, LIVE_PRICE, settings=settings())
        assert result.lp_delta_eth.quantize(Decimal("0.0001")) == Decimal("5.0276")
        assert result.coverage_ratio_eth is not None
        assert result.coverage_ratio_eth.quantize(Decimal("0.01")) == Decimal("1.40")
        assert "over-hedged" in result.flags

    def test_naked_lp_flagged(self) -> None:
        result = analyze(LP, None, LIVE_PRICE, settings=settings())
        assert result.short_size_eth == 0
        assert result.coverage_ratio_eth is None
        assert "naked-lp" in result.flags

    def test_above_range_all_short_naked_delta(self) -> None:
        """Above range LP delta = 0: any short is pure exposure."""
        result = analyze(LP, SHORT, CEILING + 50, settings=settings())
        assert result.lp_delta_eth == 0
        assert result.coverage_ratio_eth is None
        assert "over-hedged" in result.flags

    @given(
        size=st.decimals(min_value=0, max_value=20, allow_nan=False, allow_infinity=False),
        pct=st.decimals(min_value=0, max_value=1, allow_nan=False, allow_infinity=False),
    )
    def test_coverage_never_negative(self, size: Decimal, pct: Decimal) -> None:
        price = FLOOR + pct * (CEILING - FLOOR)
        short = ShortParams(
            size_eth=size,
            entry_price=Decimal("1869"),
            sl_trigger=Decimal("1925"),
            collateral_usd=Decimal("6500"),
        )
        result = analyze(LP, short, price, settings=settings())
        if result.coverage_ratio_eth is not None:
            assert result.coverage_ratio_eth >= 0


class TestSchoolRules:
    def test_break_even_sizing_makes_floor_net_zero(self) -> None:
        """THE insurance-policy invariant: S* short → net ≈ 0 at the floor."""
        result = analyze(LP, SHORT, SHORT.entry_price, settings=settings())
        star = result.break_even_short_size
        assert star is not None
        hedged = ShortParams(
            size_eth=star,
            entry_price=SHORT.entry_price,
            sl_trigger=SHORT.sl_trigger,
            collateral_usd=SHORT.collateral_usd,
        )
        [point] = simulate(LP, hedged, [FLOOR], entry_price=SHORT.entry_price)
        assert abs(point.net_usd) < Decimal("0.01")

    def test_break_even_size_less_than_total_lp_value(self) -> None:
        """Doc: 'Short is NOT equal to total LP value'."""
        result = analyze(LP, SHORT, SHORT.entry_price, settings=settings())
        assert result.break_even_short_size is not None
        star_notional = result.break_even_short_size * SHORT.entry_price
        assert star_notional < result.lp_value_usd

    def test_sl_below_q1q2_boundary_flagged(self) -> None:
        """Live SL $1925 sits at ~63% of the range — below the 75% rule."""
        result = analyze(LP, SHORT, LIVE_PRICE, settings=settings())
        assert "sl-below-q1q2-rule" in result.flags
        assert result.q1_q2_boundary_price.quantize(Decimal("1")) == Decimal("1970")

    def test_sl_correlated_with_top_exit_flag(self) -> None:
        """SL trigger below the ceiling ⇒ stop-out and top-exit are one rally."""
        result = analyze(LP, SHORT, LIVE_PRICE, settings=settings())
        assert "sl-correlated-with-top-exit" in result.flags

    def test_premium_if_sl_fires(self) -> None:
        """Premium = size * (entry - sl) < 0 — the stop-out cost."""
        result = analyze(LP, SHORT, LIVE_PRICE, settings=settings())
        expected = SHORT.size_eth * (SHORT.entry_price - Decimal("1925"))
        assert result.premium_if_sl_fires is not None
        assert result.premium_if_sl_fires.quantize(Decimal("0.01")) == expected.quantize(
            Decimal("0.01")
        )
        assert result.premium_if_sl_fires < 0


class TestSimulate:
    def test_dual_curve_shapes(self) -> None:
        prices = [FLOOR, LIVE_PRICE, SHORT.entry_price, CEILING]
        points = simulate(LP, SHORT, prices, entry_price=SHORT.entry_price)
        assert len(points) == 4
        # LP value increases with price; short PnL decreases with price.
        lp_values = [p.lp_value_usd for p in points]
        short_pnls = [p.short_pnl_usd for p in points]
        assert lp_values == sorted(lp_values)
        assert short_pnls == sorted(short_pnls, reverse=True)

    def test_net_at_entry_is_zero(self) -> None:
        [point] = simulate(LP, SHORT, [SHORT.entry_price], entry_price=SHORT.entry_price)
        assert abs(point.net_usd) < Decimal("0.01")

    @given(pct=st.decimals(min_value=0, max_value=1, allow_nan=False, allow_infinity=False))
    def test_net_is_lp_change_plus_short_pnl(self, pct: Decimal) -> None:
        price = FLOOR + pct * (CEILING - FLOOR)
        [point] = simulate(LP, SHORT, [price], entry_price=SHORT.entry_price)
        assert point.net_usd == point.lp_change_usd + point.short_pnl_usd


class TestLimits:
    def test_target_exceeding_configured_max_flagged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEXPAPRIKA_MAX_POSITION_USD", "1000")  # tiny cap
        result = analyze(LP, SHORT, LIVE_PRICE, settings=Settings.load())
        assert "target-exceeds-configured-max" in result.flags

    def test_zero_limit_means_disabled_no_flag(self) -> None:
        result = analyze(LP, SHORT, LIVE_PRICE, settings=settings())
        assert "target-exceeds-configured-max" not in result.flags
