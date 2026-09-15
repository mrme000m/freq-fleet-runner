#!/usr/bin/env python3
"""Unit tests for the pure grid geometry extracted into
execution/grid_geometry.py from grid_adapter.compute_upsert /
build_ticket_payloads.

Numeric expectations are grounded in:
  - the exact source formulas in grid_adapter.py (percent units,
    `guard < 500` cap, rounding points), and
  - tests/fixtures/grid_resource.json — a real WunderTrading grid bot
    record (bot 278981: low 83.85 / high 90.02 / step 0.59% / 13 levels,
    closest levels 86.862418225744 / 87.374906493276 at price 87.03).
No I/O, no network: everything below is pure math.
"""
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "execution"))

import pytest

from grid_geometry import (
    GUARD_CAP,
    channel,
    closest_levels,
    compute_grid,
    fee_floor_step,
    geometric_lines,
    per_line_size,
    round_trip_fee_pct,
    side_lines_count,
    worst_case_commitment,
)

# ── fixture: a real deployed grid bot (grid_resource.json) ────────────
FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "grid_resource.json")))
FX_LOW = FIXTURE["lowPrice"]          # 83.85
FX_HIGH = FIXTURE["highPrice"]        # 90.02
FX_STEP_PCT = FIXTURE["gridPercentStep"] * 100.0   # 0.0059 → 0.59 percent
FX_LEVELS = FIXTURE["gridLevels"]     # 13
FX_PRICE = FIXTURE["initPrice"]       # 87.03
FX_CLOSEST_LOW = FIXTURE["closestLowLevelPrice"]    # 86.862418225744
FX_CLOSEST_HIGH = FIXTURE["closestHighLevelPrice"]  # 87.374906493276


# ── channel math ──────────────────────────────────────────────────────

class TestChannel:
    def test_symmetric_band_around_price(self):
        low, high = channel(100.0, 2.0, band_atr=3.0)
        # band = 3 * 2 / 100 = 6%
        assert low == pytest.approx(94.0)
        assert high == pytest.approx(106.0)

    def test_high_low_mirror_price(self):
        low, high = channel(87.03, 1.7, band_atr=2.0)
        assert high - 87.03 == pytest.approx(87.03 - low)

    def test_zero_atr_collapses_to_price(self):
        low, high = channel(50.0, 0.0)
        assert low == high == 50.0

    def test_none_atr_treated_as_zero(self):
        low, high = channel(50.0, None)
        assert low == high == 50.0

    def test_default_band_atr_is_3(self):
        low, high = channel(100.0, 1.0)
        # band_atr defaults to 3.0 → ±3%
        assert low == pytest.approx(97.0)
        assert high == pytest.approx(103.0)

    def test_fixture_channel_round_trip(self):
        # the fixture bot's channel (low 83.85 / high 90.02) is symmetric
        # around its midpoint — the ATR-band formula reproduced exactly
        center = (FX_LOW + FX_HIGH) / 2.0
        band_pct = (FX_HIGH - center) / center * 100.0
        low, high = channel(center, band_pct, band_atr=1.0)
        assert low == pytest.approx(FX_LOW, abs=1e-6)
        assert high == pytest.approx(FX_HIGH)


# ── geometric line generation ─────────────────────────────────────────

class TestGeometricLines:
    def test_fixture_levels_count_and_closest(self):
        """The exact numbers of the real bot in grid_resource.json."""
        lines = geometric_lines(FX_LOW, FX_HIGH, FX_STEP_PCT)
        assert len(lines) == FX_LEVELS
        low_l, high_l = closest_levels(lines, FX_PRICE)
        assert round(low_l, 12) == pytest.approx(FX_CLOSEST_LOW, abs=1e-9)
        assert round(high_l, 12) == pytest.approx(FX_CLOSEST_HIGH, abs=1e-9)

    def test_first_line_is_low_last_is_high(self):
        lines = geometric_lines(94.0, 106.0, 1.0)
        assert lines[0] == 94.0
        assert lines[-1] == 106.0

    def test_strictly_increasing(self):
        lines = geometric_lines(FX_LOW, FX_HIGH, FX_STEP_PCT)
        assert all(b > a for a, b in zip(lines, lines[1:]))

    def test_uniform_geometric_ratio_between_consecutive_lines(self):
        lines = geometric_lines(FX_LOW, FX_HIGH, FX_STEP_PCT)
        ratio = 1 + FX_STEP_PCT / 100.0
        for a, b in zip(lines, lines[1:-1]):
            assert b / a == pytest.approx(ratio)

    def test_count_matches_closed_form(self):
        # the loop appends low*(1+s)^k while (high-e)/e*100 >= step_pct,
        # i.e. while (1+s)^k <= high/(low*(1+s)):
        #   k_max = floor(log(high/(low*(1+s))) / log(1+s))
        #   n = max(k_max + 1, 0) interior lines + the closing high
        low, high, step = 100.0, 110.0, 1.0
        s = step / 100.0
        k_max = math.floor(math.log(high / (low * (1 + s))) / math.log(1 + s))
        assert len(geometric_lines(low, high, step)) == max(k_max + 1, 0) + 1

    def test_guard_cap_limits_iterations(self):
        # step ~ 0 → the (high-e)/e*100 >= step_pct condition stays true
        # until e overshoots high; the guard < 500 cap must stop it at
        # 500 appended lines + the closing high = 501.
        lines = geometric_lines(100.0, 100.001, 1e-12)
        assert len(lines) == GUARD_CAP + 1
        assert lines[-1] == 100.001

    def test_zero_step_still_terminates_via_guard_cap(self):
        # step_pct=0 keeps the loop condition true forever → guard cap
        assert len(geometric_lines(1.0, 2.0, 0.0)) == GUARD_CAP + 1

    def test_step_wider_than_channel_yields_only_high(self):
        # (high-low)/low*100 = 1.0% < 50% → the loop never runs, so only
        # the channel high is appended (the source never special-cases low)
        assert geometric_lines(100.0, 101.0, 50.0) == [101.0]

    def test_step_fitting_exactly_once_yields_low_and_high(self):
        # (101-100)/100*100 = 1.0% >= 1.0% → low appended, then high
        assert geometric_lines(100.0, 101.0, 1.0) == [100.0, 101.0]

    def test_remaining_distance_uses_percent_vs_percent(self):
        # the loop stops when (high-e)/e*100 < step_pct, i.e. the final
        # appended line may sit less than one full step below high.
        lines = geometric_lines(100.0, 110.0, 5.0)
        assert lines[-1] == 110.0
        assert lines[-2] / 110.0 - 1 < -0.05  # last interior line >5% below


# ── closest levels ────────────────────────────────────────────────────

class TestClosestLevels:
    def test_price_at_a_line(self):
        lines = [10.0, 11.0, 12.0]
        assert closest_levels(lines, 11.0) == (11.0, 12.0)

    def test_price_above_all_lines_uses_last_as_low(self):
        assert closest_levels([10.0, 11.0], 99.0) == (11.0, 11.0)

    def test_price_below_all_lines_uses_first_as_high(self):
        assert closest_levels([10.0, 11.0], 1.0) == (10.0, 10.0)


# ── fee floor ─────────────────────────────────────────────────────────

class TestFeeFloorStep:
    def test_fee_table_matches_guardrails(self):
        assert round_trip_fee_pct("hyperliquid") == 0.10
        assert round_trip_fee_pct("binance") == 0.20
        assert round_trip_fee_pct("unknown") == 0.15  # source .get default

    def test_step_within_band_passes_through(self):
        assert fee_floor_step(1.0, 0.02, "hyperliquid") == pytest.approx(1.0)

    def test_small_step_raised_to_fee_floor(self):
        # 2*0.02 + 0.10 = 0.14 > step*mult = 0.05 → raised to 0.14
        assert fee_floor_step(0.05, 0.02, "hyperliquid") == pytest.approx(0.14)

    def test_binance_fee_floor_is_higher(self):
        # 2*0.02 + 0.20 = 0.24
        assert fee_floor_step(0.05, 0.02, "binance") == pytest.approx(0.24)

    def test_none_spread_means_fees_only(self):
        assert fee_floor_step(0.05, None, "hyperliquid") == pytest.approx(0.10)

    def test_step_mult_scales_step(self):
        assert fee_floor_step(0.5, 0.0, "hyperliquid", step_mult=2.0)             == pytest.approx(1.0)

    def test_clamped_to_step_min(self):
        # step*mult and floor both tiny → step_min (0.1) wins
        assert fee_floor_step(0.01, 0.0, "hyperliquid",
                              step_min=0.1, step_max=2.0) == pytest.approx(0.1)

    def test_clamped_to_step_max(self):
        assert fee_floor_step(99.0, 0.0, "hyperliquid",
                              step_min=0.1, step_max=2.0) == pytest.approx(2.0)

    def test_final_step_covers_round_trip_cost(self):
        """Invariant: with default bounds, the final step is never below
        2*spread + round-trip fee when the floor sits inside the band."""
        for spread in (0.0, 0.01, 0.03, 0.1):
            for venue in ("hyperliquid", "binance"):
                for step in (0.02, 0.5, 1.7):
                    got = fee_floor_step(step, spread, venue)
                    floor = 2 * spread + round_trip_fee_pct(venue)
                    if got <= 2.0:  # not clipped by step_max
                        assert got >= floor - 1e-12


# ── sizing ────────────────────────────────────────────────────────────

class TestSizing:
    def test_per_line_plain_split(self):
        assert per_line_size(1000.0, 10) == pytest.approx(100.0)

    def test_per_line_floored_at_min_cost(self):
        # $25 budget over 13 lines → ~$1.92/line < $2 exchange minimum
        assert per_line_size(25.0, 13, min_cost=2.0) == 2.0

    def test_per_line_above_min_cost_untouched(self):
        assert per_line_size(260.0, 13, min_cost=2.0) == pytest.approx(20.0)

    def test_per_line_without_min_cost(self):
        assert per_line_size(1.0, 13) == pytest.approx(1.0 / 13)

    def test_fixture_sizing_shape(self):
        # fixture bot: 417 per line, 13 levels → $5421 distributed;
        # side lines = (13+1)//2 = 7
        assert side_lines_count(FX_LEVELS) == 7
        assert per_line_size(FX_LEVELS * FIXTURE["amountPerTrade"], FX_LEVELS)             == pytest.approx(FIXTURE["amountPerTrade"])

    def test_side_lines_odd_and_even(self):
        assert side_lines_count(13) == 7
        assert side_lines_count(12) == 6
        assert side_lines_count(1) == 1

    def test_worst_case_commitment_is_one_sided(self):
        assert worst_case_commitment(417.0, 7) == pytest.approx(2919.0)

    def test_worst_case_below_total_distribution(self):
        per_line = per_line_size(1000.0, 10)
        total = worst_case_commitment(per_line, side_lines_count(10))
        assert total == pytest.approx(500.0)  # half the channel, not $1000


# ── top-level compute_grid ────────────────────────────────────────────

class TestComputeGrid:
    def test_geometry_matches_compute_upsert_payload_fields(self):
        g = compute_grid(100.0, 2.0, 1.0, band_atr=3.0)
        assert g["low"] == pytest.approx(round(94.0, 6))
        assert g["high"] == pytest.approx(round(106.0, 6))
        assert g["grids"] == len(g["lines"])
        assert g["lines"][-1] == pytest.approx(106.0)
        assert g["closest_low"] <= 100.0 <= g["closest_high"]

    def test_sizing_block(self):
        g = compute_grid(100.0, 2.0, 1.0, alloc_usd=1300.0,
                         min_cost=2.0, amount_precision=2)
        grids_n = g["grids"]
        assert g["amount_per_trade"] == pytest.approx(1300.0 / grids_n)
        assert g["side_lines"] == (grids_n + 1) // 2
        assert g["distributed_notional"] == pytest.approx(
            g["amount_per_trade"] * grids_n, abs=0.02)
        assert g["total_commitment_estimate"] == pytest.approx(
            g["amount_per_trade"] * g["side_lines"], abs=0.02)

    def test_min_cost_floor_flows_through(self):
        # $5 over many lines < $2/line → every line funded at $2
        g = compute_grid(100.0, 2.0, 0.2, alloc_usd=5.0, min_cost=2.0)
        assert g["amount_per_trade"] == 2.0
        assert g["total_commitment_estimate"] == pytest.approx(2.0 * g["side_lines"])

    def test_no_alloc_no_sizing_keys(self):
        g = compute_grid(100.0, 2.0, 1.0)
        for k in ("amount_per_trade", "side_lines",
                  "distributed_notional", "total_commitment_estimate"):
            assert k not in g

    def test_guard_cap_threaded_through(self):
        g = compute_grid(100.0, 2.0, 1e-12, guard_cap=7)
        assert len(g["lines"]) == 7 + 1  # 7 guarded iterations + high

    def test_end_to_end_fixture_reproduction(self):
        """The fixture bot's symmetric channel + step reproduce the live
        bot record exactly: 13 levels and both closest-level prices."""
        center = (FX_LOW + FX_HIGH) / 2.0
        band_pct = (FX_HIGH - center) / center * 100.0
        g = compute_grid(center, atr_pct=band_pct, step_pct=FX_STEP_PCT,
                         band_atr=1.0)
        assert g["low"] == pytest.approx(FX_LOW, abs=1e-6)
        assert g["high"] == pytest.approx(FX_HIGH, abs=1e-6)
        assert g["grids"] == FX_LEVELS
        assert g["closest_low"] == pytest.approx(FX_CLOSEST_LOW, abs=1e-6)
        assert g["closest_high"] == pytest.approx(FX_CLOSEST_HIGH, abs=1e-6)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
