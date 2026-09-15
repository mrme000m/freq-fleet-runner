#!/usr/bin/env python3
"""Pure grid geometry — the math extracted from grid_adapter.

grid_adapter.compute_upsert() and grid_adapter.build_ticket_payloads()
embed the grid-bot geometry inline: ATR-band channel, geometric grid
lines, fee-aware step floor, per-line sizing and the one-sided
worst-case commitment. This module extracts that math as pure
functions so it can be unit-tested in isolation.

No I/O, no network, no WunderTrading/browser imports, no mutable
global state. Everything replicates the source formulas EXACTLY
(percent units, `guard < 500` loop cap, rounding points) — see the
inline notes and tests/test_grid_geometry.py.

Formulas (verified against grid_adapter.py @ main and the live bot
record tests/fixtures/grid_resource.json):

  channel     band = band_atr * atr_pct / 100
              low  = price * (1 - band)
              high = price * (1 + band)

  lines       e = low; while e <= high and (high-e)/e*100 >= step_pct
              (percent vs percent) and guard < 500: append e,
              e *= (1 + step_pct/100); then append high as last line.

  fee floor   fee_floor_pct = 2*spread_pct + round_trip_fee_pct(venue,
                             taker=taker)
              step_pct = min(step_max, max(step_min, step*step_mult,
                                           fee_floor_pct))
              NOTE: unlike a naive "min" reading, the fee floor RAISES
              the step (a step below the round-trip cost loses money
              on every fill); it is never lowered to it.
              `taker=True` prices the floor at the taker round-trip fee
              (matching a config that charges fee-per-side taker); it
              defaults to False (maker) for back-compat.

  sizing      per_line  = max(alloc_usd / grids_n, min_cost)
              side_lines = max(1, (grids_n + 1) // 2)
              worst_case = per_line * side_lines
"""
try:  # single source of truth for the round-trip fee table
    from guardrails import ROUND_TRIP_FEE_PCT
except ImportError:  # package-relative import when execution is a package
    try:
        from execution.guardrails import ROUND_TRIP_FEE_PCT
    except ImportError:  # standalone: keep the same values inline
        ROUND_TRIP_FEE_PCT = {"hyperliquid": 0.10, "binance": 0.20}

# venue default used by grid_adapter.build_ticket_payloads when the
# venue is absent from ROUND_TRIP_FEE_PCT
DEFAULT_ROUND_TRIP_FEE_PCT = 0.15

# taker round-trip fee table — the fee a config that charges taker
# per-side actually pays. hyperliquid 0.10%/side = 0.20% round trip;
# binance 0.10%/side = 0.20% round trip. Used when `taker=True` so the
# fee floor matches the *charged* cost (config `fee: 0.001` = 0.1%/side)
# rather than the maker rebate the resting-limit order would earn.
ROUND_TRIP_FEE_TAKER_PCT = {"hyperliquid": 0.20, "binance": 0.20}

# taker default (2x the maker default 0.15)
DEFAULT_ROUND_TRIP_FEE_TAKER_PCT = 0.30

# iteration cap hard-coded in both grid_adapter geometry loops
GUARD_CAP = 500

__all__ = [
    "ROUND_TRIP_FEE_PCT", "DEFAULT_ROUND_TRIP_FEE_PCT",
    "ROUND_TRIP_FEE_TAKER_PCT", "DEFAULT_ROUND_TRIP_FEE_TAKER_PCT",
    "GUARD_CAP",
    "channel", "geometric_lines", "closest_levels",
    "fee_floor_step", "round_trip_fee_pct",
    "per_line_size", "side_lines_count", "worst_case_commitment",
    "compute_grid",
]


def channel(price, atr_pct, band_atr=3.0):
    """ATR-band channel around `price` -> (low, high).

    Mirrors compute_upsert / build_ticket_payloads:
      band = band_atr * (atr_pct or 0.0) / 100
      high = price * (1 + band); low = price * (1 - band)
    """
    band = band_atr * (atr_pct or 0.0) / 100.0
    return price * (1 - band), price * (1 + band)


def geometric_lines(low, high, step_pct, guard_cap=GUARD_CAP):
    """Geometric grid lines from `low` up to `high` (both inclusive).

    Exact replication of the loop in grid_adapter (compute_upsert and
    the sizing block of build_ticket_payloads — they are identical):

      step = (step_pct or 0.1) / 100          # fraction
      while e <= high and (high - e) / e * 100 >= step_pct \
              and guard < guard_cap:
          append e; e *= (1 + step); guard += 1
      append high                             # last line is the channel high

    `step_pct` is in PERCENT. The while-condition compares the remaining
    relative distance to `high` (percent) against `step_pct` (percent) —
    not a plain `e * (1 + step) <= high` test. `guard_cap` mirrors the
    source's `< 500` iteration cap (returns at most cap+1 lines).
    """
    step = (step_pct or 0.1) / 100.0
    lines, e = [], low
    guard = 0
    while e <= high and (high - e) / e * 100 >= step_pct and guard < guard_cap:
        lines.append(e)
        e *= (1 + step)
        guard += 1
    lines.append(high)
    return lines


def closest_levels(lines, price):
    """Nearest grid line at/below `price` and strictly above it.

    Mirrors compute_upsert's closest_low/closest_high:
      below = [ln <= price]; above = [ln > price]
      closest_low  = max(below) if below else lines[0]
      closest_high = min(above) if above else lines[-1]
    """
    below = [ln for ln in lines if ln <= price]
    above = [ln for ln in lines if ln > price]
    closest_low = max(below) if below else lines[0]
    closest_high = min(above) if above else lines[-1]
    return closest_low, closest_high


def round_trip_fee_pct(venue, taker=False):
    """Round-trip fee for a venue, in PERCENT (maker: hyperliquid 0.10,
    binance 0.20, anything else defaults to 0.15; taker: hyperliquid 0.20,
    binance 0.20, anything else defaults to 0.30 — grid_adapter's `.get`
    default, doubled for taker)."""
    table = ROUND_TRIP_FEE_TAKER_PCT if taker else ROUND_TRIP_FEE_PCT
    default = DEFAULT_ROUND_TRIP_FEE_TAKER_PCT if taker \
        else DEFAULT_ROUND_TRIP_FEE_PCT
    return table.get(venue, default)


def fee_floor_step(step_pct, spread_pct, venue,
                   step_min=0.1, step_max=2.0, step_mult=1.0, taker=False):
    """Fee-aware step floor — exact build_ticket_payloads formula (percent):

      rt_fee     = round_trip_fee_pct(venue, taker=taker)
      fee_floor  = 2 * (spread_pct if spread_pct is not None else 0.0)
                   + rt_fee
      step       = min(step_max, max(step_min, step_pct * step_mult,
                                     fee_floor))

    Units: spread_pct and the returned value are PERCENT; ROUND_TRIP_FEE_PCT
    values are also percent (0.10 = 0.10%), i.e. NOT divided by 100.
    `taker=True` prices the floor at the taker round-trip fee (0.20% on
    hyperliquid) so a config that charges taker per-side never models a
    floor below its real cost; defaults to False (maker) for back-compat.
    Invariant (for sane step_min/step_max): the result is >= the fee floor
    whenever the floor is within [step_min, step_max]; the floor can only
    be clipped by an unusually small step_max.
    """
    rt_fee = round_trip_fee_pct(venue, taker=taker)
    fee_floor = 2 * (spread_pct if spread_pct is not None else 0.0) + rt_fee
    return min(step_max, max(step_min, step_pct * step_mult, fee_floor))


def per_line_size(alloc_usd, grids_n, min_cost=None):
    """Per-grid-line notional in USD.

    Mirrors build_ticket_payloads: per_line = alloc_usd / grids_n, raised
    to the exchange minimum `min_cost` when the split falls below it
    ("fund density up to the exchange minimum" — grid count is never
    degraded for budget reasons).
    """
    per_line = alloc_usd / max(grids_n, 1)
    if min_cost and per_line < min_cost:
        per_line = float(min_cost)
    return per_line


def side_lines_count(grids_n):
    """Lines on the adverse side of the grid: buy-side for long/neutral,
    sell-side for short. Mirrors: max(1, (grids_n + 1) // 2)."""
    return max(1, (grids_n + 1) // 2)


def worst_case_commitment(per_line, side_lines):
    """One-sided worst-case adverse commitment: per_line * side_lines."""
    return per_line * side_lines


def compute_grid(price, atr_pct, step_pct, alloc_usd=None, min_cost=None,
                 band_atr=3.0, amount_precision=2, guard_cap=GUARD_CAP):
    """Top-level geometry + sizing — the pure core of the adapter path.

    Mirrors the geometry outputs of compute_upsert (channel, lines,
    level count, closest levels) plus the sizing outputs of
    build_ticket_payloads (per-line, side lines, worst-case commitment).
    Sizing is computed only when `alloc_usd` is positive, as in the
    source. Returns a plain dict; rounding matches the source
    (payload prices to 6 dp, amounts to `amount_precision` decimals,
    notional/commitment to 2 dp).
    """
    low, high = channel(price, atr_pct, band_atr)
    lines = geometric_lines(low, high, step_pct, guard_cap=guard_cap)
    grids_n = max(len(lines), 1)
    closest_low, closest_high = closest_levels(lines, price)
    out = {
        "low": round(low, 6),
        "high": round(high, 6),
        "lines": lines,
        "grids": grids_n,
        "closest_low": round(closest_low, 6),
        "closest_high": round(closest_high, 6),
        "band": band_atr * (atr_pct or 0.0) / 100.0,
    }
    if alloc_usd and alloc_usd > 0 and price:
        side_lines = side_lines_count(grids_n)
        per_line = per_line_size(alloc_usd, grids_n, min_cost)
        precision = int(amount_precision or 2)
        amount_usd = round(per_line, precision) or \
            round(10 ** -precision, precision)
        out.update({
            "per_line_raw": per_line,
            "amount_per_trade": amount_usd,
            "side_lines": side_lines,
            "distributed_notional": round(amount_usd * grids_n, 2),
            "total_commitment_estimate": round(
                worst_case_commitment(amount_usd, side_lines), 2),
        })
    return out
