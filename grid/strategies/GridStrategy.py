"""GridStrategy — single-position DCA grid (M1, M5 profitability pass).

Single open trade per pair. On entry (tag `grid_recenter`) the strategy
builds an ATR-channel geometric grid from the shared geometry module and
buys/sells per grid line via adjust_trade_position (tags grid_buy_Li /
grid_sell_Lj). Full profit exit at the channel top (channel_top_exit);
regime break below the channel bottom (channel_bottom_exit) cuts the
position and blocks re-entry for a cooldown.

M5 profitability pass (see docs/reliability-ledger.md §"Finding"):
  * taker-aware fee floor — `use_taker_fee=True` prices the geometry
    floor at the taker round-trip fee (0.20% hyperliquid) so the floor
    matches the charged cost (`fee: 0.001` = 0.1%/side), not the maker
    rebate. REQUIRES re-hyperopt: M2's step_factor 0.21 was maker-modeled
    and now steps clamp to 0.24% ≈ cost; the taker-viable baseline is
    step_factor 1.0 (step = full ATR%).
  * entry filter — skip entries when the ATR% is too thin for the grid
    step to clear fees (min_atr_pct floor + min_step_multiple × cost) or
    in a confirmed downtrend (close below EMA26 by trend_tolerance_pct).
  * downside exit + recenter cooldown.
  * bounded line refill (max_refills_per_line). (The M5 "fee-viable buy
    window" was superseded by the 2026-09-15 resting-ladder pass below:
    fills now land AT the line, so the step >= min_step_multiple x cost
    entry gate is itself the per-trip fee bar.)
  * confirm-then-mutate line state (2026-09-15 desync fix): `filled` /
    `refills` change ONLY in order_filled (freqtrade's fill
    confirmation) — never at adjust_trade_position decision time, where a
    decided sell can still be vetoed by freqtrade's remaining-position
    minimum or a placed order can time out unfilled. Decision-time
    mutation produced phantom sold lines, double inventory and sells of
    never-bought inventory in the live dry-run. adjust_trade_position
    derives pending (open, unfilled) grid orders from trade.orders and
    skips those lines, and pre-checks the remaining-position veto so a
    decided exit is one freqtrade will actually place.
  * grid state persists to <user_data_dir>/grid_state.json and is
    restored on bot_start so a restart mid-trade keeps its fills.
  * alloc_usd / min_cost / spread_pct are hyperoptable; alloc_usd scales
    by config["ladder_pct"] (injected by the deployer from the M4
    reliability ledger ladder).

Dynamic TF rescaling (2026-09-15 lower-TF band, 1m-5m):
  * every ATR% the strategy consumes is renormalized to the 1h reference
    horizon (ATR_REF_MINUTES) by a sqrt-of-time factor
    _atr_scale(timeframe) = sqrt(60 / tf_minutes). The tuned params
    (band_atr 4.2, min_atr_pct 0.3, the min_step_multiple x cost gate)
    are denominated in 1h-horizon volatility; raw 1m ATR% is ~sqrt(60)
    smaller, so without rescaling the absolute gates are unreachable
    (BTC/1m raw ATR% maxes ~0.14% vs the 0.36% effective gate) and the
    channel degenerates to +-4.2 x 0.06% = +-0.25% — a pinhole price
    blows through in minutes. Rescaled, a 1h slot stays bit-equal
    (factor 1.0) and a 1m-5m slot keeps the calibrated economics:
    fee-clearing steps (~0.4-0.9%) inside a sane +-1-2% channel. The
    lower-TF cadence shows up in management speed (per-candle downside
    / refill / cooldown checks), not in degenerate geometry.
  * the trend gate's EMA horizon is rescaled the same way
    (_trend_ema_period: 26 bars x 60/tf_minutes): the tuned gate is
    EMA26 on 1h = a ~26h trend. A fixed 26-bar EMA on the 1m-5m band
    spans 26-130 MINUTES — it lets slots average into a multi-day
    downtrend whenever price bounces above the micro trend (the
    2026-09-15 session: every slot 1.7-2.7% under its 26h EMA while
    the micro gate waved entries through). A 1h slot stays bit-equal
    (exactly 26 bars); startup_candle_count scales to match.

Resting-ladder harvest (2026-09-15 profitability fix):
  * grid orders are priced AT their line, not at market: a grid buy
    rests as a limit at lines[i] (custom_entry_price parses the
    grid_buy_Li tag), a grid sell rests at the line's TP
    lines[j]*(1+step) (custom_exit_price parses grid_sell_Lj). Every
    completed round trip banks the FULL step minus cost — the
    entry gate's step >= min_step_multiple x cost guarantee is what
    each trip actually earns.
  * the pre-fix design market-bought inside a fee-viable WINDOW above
    the line: in a falling tape every fill landed at the window's top
    edge, so trips harvested ~= the cost floor (observed live: +0.25%
    gross vs 0.24% cost — fee-positive by one bp, i.e. churn). Line
    fills are structurally immune: the harvest is fixed at order
    placement, not at the mercy of where inside a window the fill
    lands.
  * placement is clamp-safe: freqtrade's get_valid_price drags any
    custom price beyond custom_price_max_distance_ratio (2% default)
    of the current rate back toward market, which could reprice a TP
    below its line's cost or a deep rung above its line. Orders are
    therefore only placed on lines within _CUSTOM_PRICE_REACH of the
    current rate (buys: below rate; sells: TP above rate); deeper
    rungs enter the envelope as price descends, so the ladder slides
    with price instead of resting the whole channel depth at once.
  * freqtrade keeps at most ONE open order per trade on the adjust
    path — placing an order that differs from the resting one CANCELS
    the resting one (freqtradebot.handle_similar_open_order, REPLACE;
    observed live 2026-09-15 as a sell/sell ping-pong on BTC). So the
    strategy works a single desired order per loop and sticks with it
    while it rests: sells take precedence once their line is at/above
    the rate (harvest first), a parked sell is only swapped for a buy
    when the rate falls below its line (falling tape — keep laddering
    down), and a resting buy is never re-decided (re-placing it would
    just cancel/rebook the same rung every loop).
  * a sell vetoed by freqtrade's remaining-position minimum no longer
    blocks the loop: adjust falls through to the buy branch so the
    position can grow past the veto bar (the pre-fix shape deadlocked:
    sell decided every loop -> veto -> None -> buys never re-fired).

Geometry resolution chain — all locations hold the SAME module, kept
byte-identical by grid/tests/test_vendored_sync.py:
  1. `grid_geometry.py` vendored NEXT TO this file. freqtrade loads
     strategies via IResolver._get_valid_object inside
     `with PathModifier(module_path.parent)`, so the strategy dir is on
     sys.path while this module body executes and a plain import resolves.
  2. The legacy grid-autonomy checkout. The M3 backend copies ONLY
     GridStrategy.py + GridStrategy.json into per-instance strategy dirs
     (STRATEGY_FILES in execution/freqtrade_backend.py), so an instance
     copy has no vendored sibling and must resolve the original source.
"""
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta

try:
    from grid_geometry import (  # noqa: E402
        channel,
        closest_levels,
        fee_floor_step,
        geometric_lines,
        per_line_size,
        round_trip_fee_pct,
        side_lines_count,
    )
except ImportError:
    sys.path.insert(0, "/Volumes/ExMac/code/tradingview/go/agents/grid-autonomy")

    from execution.grid_geometry import (  # noqa: E402
        channel,
        closest_levels,
        fee_floor_step,
        geometric_lines,
        per_line_size,
        round_trip_fee_pct,
        side_lines_count,
    )

from freqtrade.persistence import Trade
from freqtrade.strategy import DecimalParameter, IStrategy

import talib.abstract as ta


# --- timeframe helpers (1m-5m band) -------------------------------------

# Map freqtrade timeframe strings to (unit, count) so a candle-count
# cooldown converts to the right wall-clock duration regardless of the
# slot's TF. The 1-5m band is fixed; if a slot is ever bumped back to
# 15m+ this still works (the table covers freqtrade's full set).
_TF_UNIT = {
    "1m": ("m", 1), "3m": ("m", 3), "5m": ("m", 5), "15m": ("m", 15),
    "30m": ("m", 30), "1h": ("h", 1), "4h": ("h", 4), "1d": ("d", 1),
}


def _tf_timedelta(tf: str, n: int):
    """N candles of TF `tf` as a timedelta — unit-aware (1m=1min,
    5m=5min, 1h=1h). Unknown TFs fall back to minutes."""
    unit, mult = _TF_UNIT.get(tf, ("m", 1))
    if unit == "m":
        return timedelta(minutes=n * mult)
    if unit == "h":
        return timedelta(hours=n * mult)
    if unit == "d":
        return timedelta(days=n * mult)
    return timedelta(minutes=n * mult)


# --- ATR reference horizon (dynamic TF rescaling) -------------------------

# The tuned params (band_atr 4.2, min_atr_pct 0.3, the min_step_multiple
# x cost entry gate) are denominated in 1h-horizon volatility — they were
# calibrated against ATR(14) on 1h candles (M2) and re-based on the same
# horizon for the M5 taker pass. On the 1m-5m band raw ATR% is smaller by
# ~sqrt(tf/1h) (variance grows with time), so a 1m slot without rescaling
# can never reach the absolute floors (BTC/1m raw ATR% tops ~0.14% vs
# the 0.36% effective gate) and its channel collapses to a pinhole
# (+-4.2 x 0.06%). Renormalizing every ATR% to this reference horizon
# restores the calibrated economics at ANY timeframe; a 1h slot stays
# bit-equal (factor exactly 1.0) and 4h slots scale down symmetrically.
ATR_REF_MINUTES = 60.0


def _tf_minutes(tf: str) -> float:
    """One candle of TF `tf` in minutes — unit-aware (1m=1, 5m=5, 1h=60)."""
    return _tf_timedelta(tf, 1).total_seconds() / 60.0


def _atr_scale(tf: str) -> float:
    """sqrt-of-time factor renormalizing a TF's ATR% to the 1h reference
    horizon: sqrt(ATR_REF_MINUTES / tf_minutes). 1m -> 7.746, 3m -> 4.472,
    5m -> 3.464, 1h -> exactly 1.0 (the calibrated regime), 4h -> 0.5."""
    return math.sqrt(ATR_REF_MINUTES / max(_tf_minutes(tf), 1e-9))


# Tuned trend horizon: EMA26 on 1h candles == a ~26h trend. The gate's
# semantics ("don't average a long grid into a multi-day downtrend") are
# denominated in HOURS, not bars — rescale the period so every slot TF
# measures the same horizon (1m -> 1560, 3m -> 520, 5m -> 312, 1h -> 26).
_TREND_EMA_REF_PERIOD = 26


def _trend_ema_period(tf: str) -> int:
    """EMA period carrying the 1h-calibrated 26-bar trend horizon onto
    the slot's TF. 1h -> exactly 26 (bit-equal to the tuned regime)."""
    return max(2, round(_TREND_EMA_REF_PERIOD
                        * ATR_REF_MINUTES / max(_tf_minutes(tf), 1e-9)))


_GRID_BUY_TAG_RE = re.compile(r"grid_buy_L(\d+)$")
_GRID_SELL_TAG_RE = re.compile(r"grid_sell_L(\d+)$")
# the min-exit-veto full-position exit. Deliberately NOT matched by the
# ledger's grid_(buy|sell)_Li regex — pairing reads it as the trade's
# full exit, one full_exit trip per lot, no pool-drain surprise.
_GRID_FLATTEN_TAG_RE = re.compile(r"grid_flatten_L(\d+)$")


class GridStrategy(IStrategy):
    INTERFACE_VERSION = 3

    # Default slot TF is 1m (lower-TF band 1m-5m per the 2026-09-15 reset).
    # Per-slot TF is overridden by the deployer's `timeframe` config key
    # (grid/dev pins BTC=1m, ETH=3m, SOL=3m, HYPE=5m), so this default
    # only matters for backtests / hyperopt / smoke runs.
    timeframe = "1m"
    # startup_candle_count is set per instance in __init__ from the
    # slot's TF (the TF-rescaled trend EMA needs its full period warm:
    # 1560+ bars on 1m, 26+40 on 1h). The class default only covers a
    # bare EMA26 + ATR14 warm-up.
    startup_candle_count = 70
    process_only_new_candles = False
    can_short = False

    # --- geometry knobs (hyperoptable, space=buy) ---
    band_atr = DecimalParameter(1.5, 5.0, default=3.0, decimals=1, space="buy")
    # step derives from ATR like the daemon: step = min(step_max,
    # max(step_min, atr_pct * step_factor)), then the fee floor inside
    # fee_floor_step still binds (structural invariant, not tuned away).
    # Default 1.0 is the taker-viable baseline (step = full ATR%); the
    # M2-tuned 0.21 was maker-modeled and must be re-hyperopted for taker.
    step_factor = DecimalParameter(0.2, 1.0, default=1.0, decimals=2,
                                   space="buy")
    # --- entry-filter knobs (hyperoptable, space=buy) ---
    min_atr_pct = DecimalParameter(0.0, 2.0, default=0.3, decimals=1,
                                   space="buy")
    # the grid step must clear min_step_multiple × the round-trip cost,
    # else the harvest is fee-degenerate chop
    min_step_multiple = DecimalParameter(1.0, 3.0, default=1.5, decimals=1,
                                         space="buy")
    # allow entries up to this far (%) below EMA26 before calling it a
    # downtrend; 0 = strict (close must be >= EMA26)
    trend_tolerance_pct = DecimalParameter(0.0, 2.0, default=0.5, decimals=1,
                                           space="buy")
    # --- sizing knobs (hyperoptable, space=buy) ---
    alloc_usd = DecimalParameter(20.0, 500.0, default=100.0, decimals=0,
                                 space="buy")
    min_cost = DecimalParameter(5.0, 50.0, default=10.0, decimals=1,
                                space="buy")
    spread_pct = DecimalParameter(0.0, 0.10, default=0.02, decimals=2,
                                  space="buy")

    # Hard ceiling on the ONE-SIDED worst-case commitment, as a multiple of
    # the (ladder-scaled) allocation. The exchange minimum per line
    # (`min_cost`) RUINS naive allocation: per_line_size() lifts every line
    # to `min_cost`, so at alloc $25 (base tier) x ~4 adverse-side lines x
    # $10 the grid silently committed $40 — 1.6x its entire budget — and
    # every ladder tier sized identically. Capital at risk is now bounded
    # and the tiers genuinely differ (see _armed_rungs).
    max_commit_ratio = 1.0

    # --- fixed economics / structural knobs ---
    venue = "hyperliquid"
    use_taker_fee = True  # config charges taker per side; floor must match
    step_min = 0.1  # grid_defaults parity (fee_floor_step defaults)
    step_max = 2.0
    max_refills_per_line = 2  # bounded re-buys of a sold line per trade
    # candles to wait before re-entering after a channel-bottom exit.
    # Counted in the slot's own timeframe (1m / 3m / 5m), not in hours —
    # the prior `timedelta(hours=...)` made the cooldown wrong by 60-300x
    # on the lower-TF band.
    recenter_cooldown_candles = 6
    # custom-price placement envelope: freqtrade's get_valid_price clamps
    # a custom order price to within custom_price_max_distance_ratio of
    # the current rate (2% default). Grid orders are only placed when
    # their line price sits inside this envelope (1.9% — the default
    # with margin), so the exchange order lands AT the line/TP instead
    # of silently repriced toward market (a clamped TP could price
    # below its line's cost; a clamped deep rung above its line).
    _CUSTOM_PRICE_REACH = 0.019

    minimal_roi = {"0": 100}
    stoploss = -0.25
    position_adjustment_enable = True
    max_entry_position_adjustment = -1

    def __init__(self, config=None):
        super().__init__(config)
        self._grids: dict = {}
        # pair -> datetime: block new entries until this candle timestamp
        # (downside-exit cooldown so we don't instantly recenter a break)
        self._recenter_block_until: dict = {}
        # order_ids whose fill effect order_filled has already applied
        # (order_filled fires once per order; the guard is just belt and
        # braces against double invocation in odd exchange paths)
        self._applied_fills: set = set()
        # Warm-up covers the TF-rescaled trend EMA. The slot TF comes
        # from the config's "timeframe" when the deployer pins one (the
        # resolver overrides self.timeframe from the same key AFTER
        # construction, so reading config here sees the final value).
        slot_tf = (self.config or {}).get("timeframe") or self.timeframe
        self.startup_candle_count = max(
            type(self).startup_candle_count,
            _trend_ema_period(slot_tf) + 40)

    # --- helpers -----------------------------------------------------

    # Sanity band for the injected ladder. The deployer only ever writes
    # 0.0 (kill) or 0.25/0.40/0.50; anything outside [0, 1] is a corrupt
    # deploy (NaN, inf, a negative, or "25" meaning 25%), never a licence
    # to allocate more.
    LADDER_MAX_PCT = 1.0

    def _ladder_pct_cfg(self) -> float:
        """ladder_pct injected by the deployer (M4 ladder): 0.25/0.40/0.50
        scale alloc; 0.0 = kill gate (stop new entries); absent → 1.0.

        FAIL-CLOSED on corrupt input. The old `except: return 1.0` turned
        an unreadable/corrupt ladder into the LARGEST possible allocation
        (2x the documented full tier of 0.50) and bypassed the kill gate
        entirely — the single most dangerous failure mode in the file.
        Absent still means 1.0 (no ladder deployed: backtest / hyperopt /
        smoke runs must keep trading).
        """
        raw = (self.config or {}).get("ladder_pct")
        if raw is None:
            return 1.0
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(val) or val < 0.0 or val > self.LADDER_MAX_PCT:
            return 0.0
        return val

    def _ladder_killed(self) -> bool:
        """M4 kill gate: the archetype is measured AND recently
        unprofitable, or the ladder config is corrupt → take no new risk."""
        return self._ladder_pct_cfg() <= 0.0

    def _alloc_usd(self) -> float:
        """Grid notional, scaled by the deployer's ladder_pct (M4 ledger
        ladder: base 0.25 / probe 0.40 / full 0.50), default 1.0."""
        return float(self.alloc_usd.value) * self._ladder_pct_cfg()

    def _cost_floor(self) -> float:
        """Round-trip cost the config actually charges, in PERCENT:
        2×spread + round-trip fee (taker by default)."""
        return 2 * float(self.spread_pct.value) + round_trip_fee_pct(
            self.venue, taker=self.use_taker_fee)

    def _trip_economics_ok(self, step_pct: float, strict: bool = True
                           ) -> bool:
        """Validate that one grid round trip is actually profitable.

        A trip buys at lines[i] and sells at lines[i]*(1+step/100), so the
        GROSS harvest is exactly `step_pct` percent. It is profitable only
        when step_pct exceeds the round-trip cost (spread + fees); the
        entry gate additionally requires min_step_multiple x cost so the
        edge is not one bad tick away from churn.

        `strict=False` relaxes to the hard break-even bar only.

        Guards the two ways a live grid can end up with a loss-making step
        even though the entry gate was sound when the grid was built:
          * grid state restored from disk after a param change / downgrade;
          * params edited (or hyperopt space re-tuned) mid-trade.
        """
        if not (math.isfinite(step_pct) and step_pct > 0.0):
            return False
        cost = self._cost_floor()
        if not (math.isfinite(cost) and cost >= 0.0):
            return False
        bar = cost * (float(self.min_step_multiple.value) if strict else 1.0)
        return step_pct > bar

    def _finite_pos(self, *values) -> bool:
        """Every value is a finite number — guards NaN/inf/None leaking out
        of an indicator warm-up (ta.ATR/ta.EMA produce NaN for the first
        `timeperiod` candles) or a corrupt state file into the geometry,
        where NaN silently collapses the channel to [nan, nan] and poisons
        every downstream price and stake."""
        for v in values:
            try:
                f = float(v)
            except (TypeError, ValueError):
                return False
            if not math.isfinite(f):
                return False
        return True

    def _safe_float(self, value, default=0.0) -> float:
        """float() that treats NaN/inf/None as `default`.

        Indicator warm-up rows are NaN, and NaN is TRUTHY in Python — the
        old `float(x) if x else 0.0` guard passed NaN straight into the
        geometry, where it produced a NaN channel and NaN order prices.
        """
        try:
            f = float(value)
        except (TypeError, ValueError):
            return default
        return f if math.isfinite(f) else default

    def _build_grid(self, pair: str, price: float, atr_pct: float) -> dict:
        """Compute channel + lines and store grid state for `pair`."""
        # Degenerate-input guard: a non-finite or non-positive price/ATR
        # (indicator warm-up, a zero/NaN candle, a bad state file) would
        # previously build a NaN channel and hand NaN prices to the
        # exchange. Fall back to a single-line grid at `price` so the
        # caller can still place a sane order instead of a poisoned one.
        if not self._finite_pos(price) or price <= 0.0:
            price = 1.0
        if not self._finite_pos(atr_pct) or atr_pct < 0.0:
            atr_pct = 0.0
        raw_step_pct = atr_pct * self.step_factor.value
        s = fee_floor_step(raw_step_pct, self.spread_pct.value, self.venue,
                           step_min=self.step_min, step_max=self.step_max,
                           taker=self.use_taker_fee)
        low, high = channel(price, atr_pct, band_atr=self.band_atr.value)
        lines = geometric_lines(low, high, s)
        state = {
            "lines": lines,
            "step_pct": s,
            "low": low,
            "high": high,
            "filled": set(),
            # line_idx -> times sold (drives the bounded-refill gate)
            "refills": {},
        }
        self._grids[pair] = state
        self._save_grid_state()
        return state

    def _grid_state(self, pair: str):
        return self._grids.get(pair)

    def _per_line_stake(self, state: dict) -> float:
        return per_line_size(self._alloc_usd(), max(len(state["lines"]), 1),
                             self.min_cost.value)

    def _armed_rungs(self, state: dict, per_line: float) -> int:
        """How many adverse-side rungs the allocation can ACTUALLY fund.

        Capital-aware ladder depth (the dynamic-adaptation fix for the
        inert-ladder defect). per_line_size() floors every line at the
        exchange minimum, so a dense grid at a low ladder tier used to
        commit far more than it was allocated: the budget is the ceiling,
        and when `min_cost` x full side depth does not fit we arm FEWER
        rungs rather than over-commit capital (we never shrink an order
        below the exchange minimum — the order would simply be rejected).

        Returns 0 when even ONE rung is unaffordable: the caller must
        place no grid order at all rather than blow the budget.
        """
        budget = self._alloc_usd() * float(self.max_commit_ratio)
        if not self._finite_pos(per_line, budget) or per_line <= 0.0:
            return 0
        if budget < per_line:
            return 0
        affordable = int(budget // per_line)
        side = side_lines_count(len(state["lines"]))
        return max(1, min(side, affordable))

    def _lines_below_mid(self, state: dict) -> int:
        mid = (state["low"] + state["high"]) / 2.0
        if not self._finite_pos(mid):
            return 1
        return sum(1 for ln in state["lines"] if ln < mid)

    # --- order-confirmed line bookkeeping (desync fix) -----------------

    def _grid_tag_line(self, order) -> int | None:
        """Line index encoded in a grid order tag (grid_buy_Li /
        grid_sell_Lj); None for every other order (the initial
        grid_recenter entry, channel exits, stoploss)."""
        tag = getattr(order, "ft_order_tag", None) or ""
        if not (tag.startswith("grid_buy_L") or tag.startswith("grid_sell_L")):
            return None
        try:
            return int(tag.rsplit("L", 1)[1])
        except (IndexError, ValueError):
            return None

    def _pending_line_orders(self, trade) -> dict:
        """line index -> {open order sides} from the trade's own order
        book. freqtrade calls adjust_trade_position every loop even while
        orders rest open, so without this a resting (unfilled) grid order
        would be re-decided and duplicated on the next candle."""
        pending: dict = {}
        for o in getattr(trade, "orders", None) or []:
            if not getattr(o, "ft_is_open", False):
                continue
            idx = self._grid_tag_line(o)
            if idx is None:
                continue
            pending.setdefault(idx, set()).add(getattr(o, "ft_order_side", ""))
        return pending

    def _min_exit_stake(self, min_stake) -> float:
        """freqtrade's remaining-position minimum for a partial exit.

        freqtradebot.check_and_call_adjust_trade_position vetoes any
        reduce whose remaining position falls below
        get_min_pair_stake_amount(pair, exit_rate, stoploss, leverage) —
        the pair minimum scaled by the stoploss reserve 1/(1-|stoploss|)
        (exchange.py _get_stake_amount_limit). The `min_stake` handed to
        adjust_trade_position is the ENTRY-side minimum (stoploss 0.0),
        so the exit-side bar is that value rescaled; estimate 2% high —
        over-skipping merely retries a later candle, and with
        confirm-then-mutate a missed estimate can no longer desync line
        state."""
        if not min_stake:
            return 0.0
        # guard: a stoploss of -1.0 (or worse) makes the reserve factor
        # 1/(1-|sl|) blow up to inf/ZeroDivisionError
        denom = 1.0 - abs(float(self.stoploss or 0.0))
        if denom <= 1e-9:
            return float(min_stake)
        return float(min_stake) / denom * 1.02

    def _sell_viable(self, trade, per_line, current_exit_rate, min_stake,
                     max_stake) -> bool:
        """Would freqtrade actually place this partial exit? Mirrors the
        veto computation in freqtradebot (proportional base amount, then
        remaining-position minimum) so adjust_trade_position never
        returns a decision the bot silently drops."""
        bar = self._min_exit_stake(min_stake)
        if not bar or not trade.stake_amount:
            return True
        sell_stake = min(per_line, max_stake)
        amount = sell_stake * trade.amount / trade.stake_amount
        remaining = (trade.amount - amount) * current_exit_rate
        # A full close is always allowed. Compare with a tolerance: float
        # arithmetic on (amount - amount) leaves ~1e-13 dust, and the old
        # exact `remaining == 0.0` test then sent a full close through the
        # remaining-position bar, where it could be vetoed and deadlock
        # the loop (pre-fall-through shape).
        if abs(remaining) <= max(1e-9, abs(bar) * 1e-6):
            return True
        return remaining >= bar

    # --- grid-state persistence (live/dry-run restarts) --------------

    def _persist_enabled(self) -> bool:
        """Only persist in live/dry-run; backtest/hyperopt rebuild state
        from scratch each run and must not write side-files."""
        try:
            from freqtrade.enums.runmode import TRADE_MODES
            return self.config.get("runmode") in TRADE_MODES
        except Exception:
            return False

    def _grid_state_path(self):
        """Resolve a per-instance writable path for grid_state.json.

        Walks up from this strategy file's directory until it finds the
        freqtrade user_data root (anything containing `user_data.json`
        or `strategies/` with a sibling `notebooks/` is the standard
        layout). Persisting there keeps the state out of the M3 backend's
        strategy-copy path (STRATEGY_FILES only copies the strategy
        source files), and survives rematerialize() because
        user_data/ is never overwritten by grid/dev.

        Falls back to:
          1. config['user_data_dir'] (some forks inject it)
          2. the strategy file's own directory (last resort)
        """
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            cur = here
            for _ in range(6):  # walk up to 6 levels
                if os.path.isdir(cur) and (
                    os.path.isfile(os.path.join(cur, "user_data.json"))
                    or (os.path.isdir(os.path.join(cur, "strategies"))
                        and os.path.isdir(os.path.join(cur, "notebooks")))
                ):
                    return os.path.join(cur, "grid_state.json")
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                cur = parent
        except Exception:
            pass
        ud = (self.config or {}).get("user_data_dir")
        if ud:
            return os.path.join(ud, "grid_state.json")
        try:
            return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "grid_state.json")
        except Exception:
            return None

    def _save_grid_state(self):
        if not self._persist_enabled():
            return
        path = self._grid_state_path()
        if not path:
            return
        payload = {}
        for pair, st in self._grids.items():
            payload[pair] = {
                "lines": st["lines"],
                "step_pct": st["step_pct"],
                "low": st["low"],
                "high": st["high"],
                "filled": sorted(st["filled"]),
                "refills": {str(k): v for k, v in st.get("refills", {}).items()},
            }
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, path)
        except OSError:
            pass

    def _load_grid_state(self) -> dict:
        path = self._grid_state_path()
        if not path or not os.path.isfile(path):
            return {}
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def bot_start(self, **kwargs) -> None:
        """Restore grid state for every pair a prior run persisted, so a
        restart mid-trade keeps its filled lines / refill counts instead
        of silently re-anchoring."""
        for pair, snap in self._load_grid_state().items():
            try:
                state = {
                    "lines": list(snap["lines"]),
                    "step_pct": float(snap["step_pct"]),
                    "low": float(snap["low"]),
                    "high": float(snap["high"]),
                    "filled": {int(i) for i in snap.get("filled", [])},
                    "refills": {int(k): int(v)
                                for k, v in snap.get("refills", {}).items()},
                }
                if not self._grid_state_sane(state):
                    # A corrupt/partial snapshot (interrupted write, an
                    # older schema, NaN lines) would otherwise be trusted
                    # for the whole session: every grid price and stake
                    # derives from it. Drop the pair and let it re-anchor
                    # on the next entry instead.
                    continue
                self._grids[pair] = state
            except (KeyError, TypeError, ValueError):
                continue

    def _grid_state_sane(self, state: dict) -> bool:
        """Structural validation of a (built or restored) grid state.

        Rejects the three ways a state can be well-formed JSON but
        financially poisonous: no lines, non-finite/non-positive prices,
        or a non-positive step (which makes every TP sit at or below its
        buy line — a guaranteed loss per round trip).
        """
        lines = state.get("lines") or []
        if not lines:
            return False
        if not all(self._finite_pos(ln) and ln > 0.0 for ln in lines):
            return False
        if not self._finite_pos(state.get("low"), state.get("high")):
            return False
        step = state.get("step_pct")
        return self._finite_pos(step) and float(step) > 0.0

    # --- freqtrade interface ----------------------------------------

    def populate_indicators(self, dataframe, metadata):
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        # atr_pct is the REFERENCE-HORIZON (1h-equivalent) ATR%: the raw
        # per-bar ATR% rescaled by _atr_scale(self.timeframe) so every
        # ATR-denominated decision (entry gates, channel band, step
        # size) carries the 1h-calibrated economics on any slot TF.
        # Raw per-bar vol stays recoverable as atr/close*100.
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"] * 100 \
            * _atr_scale(self.timeframe)
        dataframe["ema_short"] = ta.EMA(dataframe, timeperiod=12)
        # trend horizon rescale (26h-equivalent on every slot TF) — see
        # _trend_ema_period. On 1h this is exactly EMA26, the tuned gate.
        dataframe["ema_long"] = ta.EMA(
            dataframe, timeperiod=_trend_ema_period(self.timeframe))
        return dataframe

    def _entry_gate_atr(self) -> float:
        """Minimum reference-horizon ATR% the entry filter requires:
        the absolute floor (min_atr_pct) or the fee-clearance bar
        (min_step_multiple x round-trip cost / step_factor), whichever
        is higher."""
        req_atr = self.min_step_multiple.value * self._cost_floor() / \
            max(self.step_factor.value, 1e-6)
        return max(self.min_atr_pct.value, req_atr)

    def populate_entry_trend(self, dataframe, metadata):
        # volatility gate (reference-horizon ATR%): the grid step must
        # clear fees by a healthy margin (min_step_multiple x cost) and
        # clear an absolute ATR floor
        vol_ok = dataframe["atr_pct"] >= self._entry_gate_atr()
        # trend gate: skip confirmed downtrends (close below EMA26 by more
        # than the tolerance); a long grid loses money averaging into them
        trend_ok = dataframe["close"] >= dataframe["ema_long"] * \
            (1 - self.trend_tolerance_pct.value / 100.0)
        dataframe.loc[
            (dataframe["volume"] > 0) & vol_ok & trend_ok,
            ["enter_long", "enter_tag"],
        ] = (1, "grid_recenter")
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        return dataframe

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float,
                            rate: float, time_in_force: str,
                            current_time: datetime, entry_tag: str | None,
                            side: str, **kwargs) -> bool:
        if self._ladder_pct_cfg() <= 0.0:
            # M4 kill gate: measured AND recently unprofitable — stop new
            # entries entirely (ladder_pct 0.0 injected by the deployer).
            return False
        block_until = self._recenter_block_until.get(pair)
        if block_until is not None and current_time < block_until:
            return False
        return True

    def custom_entry_price(self, pair: str, trade, current_time: datetime,
                           proposed_rate: float, entry_tag: str | None,
                           side: str, **kwargs) -> float:
        if trade is not None:
            # Position-adjustment order (grid buy): rest AT the line so a
            # fill banks the full step to TP, not whatever window position
            # the market happened to be at (see the module docstring's
            # resting-ladder note). Never rebuild the grid here — that
            # would wipe filled/refill sets.
            m = _GRID_BUY_TAG_RE.match(entry_tag or "")
            state = self._grid_state(pair)
            if m and state:
                idx = int(m.group(1))
                if idx < len(state["lines"]):
                    return state["lines"][idx]
            return proposed_rate
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is None or len(dataframe) == 0:
            # No analyzed dataframe (first candle, a reload race, a pair
            # the bot just dropped): `dataframe.iloc[-1]` raised IndexError
            # and killed the entry callback. Fall back to the rate
            # freqtrade already priced rather than crashing the loop.
            return proposed_rate
        last = dataframe.iloc[-1]
        close = self._safe_float(last.get("close"))
        atr_pct = self._safe_float(last.get("atr_pct"), default=0.0)
        if not (close and close > 0.0):
            return proposed_rate
        state = self._build_grid(pair, close, atr_pct)
        line, _above = closest_levels(state["lines"], proposed_rate)
        return line if self._finite_pos(line) and line > 0.0 \
            else proposed_rate

    def custom_exit_price(self, pair: str, trade: Trade,
                          current_time: datetime, proposed_rate: float,
                          current_profit: float, exit_tag: str | None,
                          **kwargs) -> float:
        # grid_sell_Lj partial exits rest at the line's TP
        # (lines[j] x (1 + step)): the trip banks exactly the step when
        # price touches it, instead of waiting for a loop iteration that
        # still finds rate >= TP. grid_flatten_Lj (the min-exit-veto
        # fallback in adjust_trade_position) sells the WHOLE position at
        # lines[j]'s TP. Full exits (channel_top/bottom_exit, stoploss,
        # roi) carry no L-tag and keep the proposed rate.
        m = _GRID_SELL_TAG_RE.match(exit_tag or "")
        state = self._grid_state(pair)
        if m and state:
            idx = int(m.group(1))
            if idx < len(state["lines"]):
                return state["lines"][idx] * (1 + state["step_pct"] / 100.0)
        mf = _GRID_FLATTEN_TAG_RE.match(exit_tag or "")
        if mf and state:
            idx = int(mf.group(1))
            if idx < len(state["lines"]):
                return state["lines"][idx] * (1 + state["step_pct"] / 100.0)
        return proposed_rate

    def custom_stake_amount(self, pair: str, current_time: datetime,
                            proposed_stake: float, min_stake: float | None,
                            max_stake: float, leverage: float,
                            entry_tag: str | None, side: str,
                            **kwargs) -> float:
        state = self._grid_state(pair)
        if state is None:
            # entry_price callback did not run (should not happen); build
            # from proposed rate with a neutral atr_pct fallback.
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe is None or len(dataframe) == 0:
                return min_stake or 0.0
            last = dataframe.iloc[-1]
            atr_pct = self._safe_float(last.get("atr_pct"), default=0.0)
            close = self._safe_float(last.get("close"))
            state = self._build_grid(pair, close if close else 1.0, atr_pct)
        stake = self._per_line_stake(state)
        if not self._finite_pos(stake) or stake <= 0.0:
            return min_stake or 0.0
        lo = min_stake or 0.0
        return max(lo, min(stake, max_stake))

    def adjust_trade_position(self, trade: Trade, current_time: datetime,
                              current_rate: float, current_profit: float,
                              min_stake: float | None, max_stake: float,
                              current_entry_rate: float,
                              current_exit_rate: float,
                              current_entry_profit: float,
                              current_exit_profit: float,
                              **kwargs):
        """Decide the next grid order for an open trade. DECISION ONLY:
        this method must never mutate `filled` / `refills` — a decided
        order can still be vetoed by freqtrade (remaining-position
        minimum) or time out unfilled, and mutating here desynced line
        state from real inventory (2026-09-15 dry-run: 8 vetoed sells,
        phantom refills, sells of never-bought lines). Line state moves
        only in order_filled, on freqtrade's fill confirmation."""
        state = self._grid_state(trade.pair)
        if state is None:
            return None
        # M4 kill gate / corrupt ladder: confirm_trade_entry only blocks NEW
        # trades, so a position opened before the kill (or under a corrupt
        # ladder) kept DCA-ing into a regime the ledger had already
        # condemned. Averaging into a losing archetype is the single most
        # expensive thing this strategy can do.
        if self._ladder_killed():
            return None
        lines = state["lines"]
        filled = state["filled"]
        refills = state["refills"]
        per_line = self._per_line_stake(state)
        # Sizing policy (GridStrategy-level, geometry module untouched):
        # with many grid lines (small step) the pure ladder size
        # (alloc/nlines, floored at min_cost) can fall below the
        # exchange/freqtrade min_stake gate, which would silently skip
        # every line. Lift the order to min_stake instead — the ladder
        # geometry (one order per line) is unchanged, only the notional.
        if min_stake and per_line < min_stake:
            per_line = min_stake
        step = state["step_pct"]
        pending = self._pending_line_orders(trade)
        reach = self._CUSTOM_PRICE_REACH
        pending_buys = {i for i, sides in pending.items() if "buy" in sides}
        pending_sells = {i for i, sides in pending.items() if "sell" in sides}

        # freqtrade keeps at most ONE open order per trade on this path:
        # placing an order that differs from the resting one CANCELS the
        # resting one (freqtradebot.handle_similar_open_order, REPLACE).
        # So this method works a single desired order at a time, sticks
        # with it while it rests (no ping-pong replaces), and only swaps
        # sides when the tape actually moved:
        #   sell -> buy: rate fell below the parked sell's line (falling
        #     tape — keep laddering down; the sell re-parks on recovery)
        #   buy -> sell: a filled line's TP came into reach (rate >= the
        #     line) — harvesting outranks a buy resting further below.

        # --- desired GRID SELL: lowest filled line whose TP is both
        # clamp-safe (within freqtrade's 2% custom-price envelope — else
        # get_valid_price would drag it toward market, possibly below
        # the line's cost) and near enough to matter (rate >= line). ---
        best_j = None
        for j, ln in enumerate(lines):
            tp = ln * (1 + step / 100.0)
            if (j in filled and current_exit_rate >= ln
                    and current_exit_rate * (1 + reach) >= tp):
                best_j = j
                break  # lowest line first — the nearest TP fills soonest
        if best_j is not None and best_j not in pending_sells:
            if self._sell_viable(trade, per_line, current_exit_rate,
                                 min_stake, max_stake):
                stake = -min(per_line, max_stake)
                return (stake, f"grid_sell_L{best_j}")
            # freqtrade would veto the one-line reduce below its
            # remaining-position minimum (the 2-lot trap: selling one
            # ~min_stake lot leaves ~1 lot < the bar, so a small position
            # can never partially exit — and with a capital-bounded
            # ladder depth the buy branch below may ALSO be budget-full,
            # which would park the position until the channel exits).
            # Flatten the WHOLE position at the HIGHEST filled line's TP
            # instead: every lot banks at least (its line -> top TP),
            # structurally fee-positive. The `grid_flatten_Li` tag
            # deliberately does NOT match the ledger's grid_(buy|sell)_Li
            # regex: the M4 pairing treats it as the trade's full exit —
            # one clean full_exit trip per lot, no state-anomaly surprise.
            top = max(filled) if filled else None
            if top is not None and "sell" not in pending.get(top, ()):
                tp_top = lines[top] * (1 + step / 100.0)
                if (current_exit_rate >= lines[top]
                        and current_exit_rate * (1 + reach) >= tp_top):
                    return (-float(trade.stake_amount),
                            f"grid_flatten_L{top}")
            # otherwise keep the lines filled and FALL THROUGH to the buy
            # branch — more inventory is exactly what lifts the veto
            # (returning None here deadlocked the grid: the decided sell
            # re-fired every loop and buys never ran again).

        # --- desired GRID BUY: resting limit AT the line (ladder) ---
        # Depth is capital-aware AND geometry-aware (see _armed_rungs): the
        # ladder arms as many adverse-side rungs as the ladder-scaled
        # allocation can actually fund at >= min_cost. Without this the
        # min_cost floor made every tier commit the same (and, at base
        # tier, ~1.6x its own budget).
        armed = self._armed_rungs(state, per_line)
        max_buys = min(self._lines_below_mid(state), armed)
        if len(filled | pending_buys) >= max_buys:
            return None
        if current_entry_rate > lines[-1]:
            # price above the channel top: channel_top_exit fires this very
            # candle, so buying now would be exited flat in the same candle
            return None
        # Profit validation on the round trip about to be opened. The entry
        # gate guarantees step >= min_step_multiple x cost at BUILD time,
        # but a grid restored from disk or re-parameterised mid-trade can
        # carry a step that no longer clears fees — and a loss-making step
        # is churn that pays the venue on every fill. Strict bar first
        # (configured margin), hard break-even backstop second so a
        # mis-set min_step_multiple can never veto a profitable trip while
        # a genuinely fee-negative one is always refused.
        if not (self._trip_economics_ok(step)
                or self._trip_economics_ok(step, strict=False)):
            return None
        # Highest eligible line in the placement envelope:
        #   upper edge — at most half a step ABOVE the rate: a marketable
        #     limit at the line fills immediately when price gapped
        #     through the line between loops (the rung is still captured
        #     at the line price, harvest still the full step);
        #   lower edge — at most _CUSTOM_PRICE_REACH below the rate:
        #     deeper rungs would be clamped by get_valid_price toward
        #     market; they enter the envelope as price descends, so the
        #     ladder slides with price a couple of rungs deep.
        best_i = None
        upper_rate = current_entry_rate * (1 + step / 2 / 100.0)
        lower_rate = current_entry_rate * (1 - reach)
        for i, ln in enumerate(lines):
            if i in filled or i in pending_buys:
                continue
            n_refills = refills.get(i, 0)
            if n_refills >= self.max_refills_per_line:
                continue
            if lower_rate <= ln <= upper_rate:
                if best_i is None or lines[best_i] < ln:
                    best_i = i
        if best_i is None:
            return None
        # single-order discipline: never stack or churn resting orders
        if pending_buys:
            # a buy is already working a rung; re-deciding it would cancel
            # and re-place it every loop (REPLACE) without changing odds
            return None
        if pending_sells:
            # a TP sell is parked above; only swap it for a buy when the
            # rate fell below the parked sell's line — price oscillating
            # above the line keeps the harvest parked instead
            parked = min(pending_sells)
            if current_entry_rate >= lines[parked]:
                return None
        # reserve room for the pending buys' notional too — their stake
        # is not in trade.stake_amount until they fill
        new_total = trade.stake_amount + per_line * (1 + len(pending_buys))
        if max_stake and new_total > max_stake:
            return None
        return (per_line, f"grid_buy_L{best_i}")

    def order_filled(self, pair: str, trade: Trade, order, current_time: datetime,
                     **kwargs) -> None:
        """Confirm-then-mutate: apply a grid fill to the line state only
        here, where freqtrade reports the order actually filled (entry,
        exit and position-adjustment orders alike — live, dry-run and
        backtest all route through this callback)."""
        state = self._grid_state(pair)
        if state is None:
            return
        oid = getattr(order, "order_id", None)
        if oid is not None:
            if oid in self._applied_fills:
                return
            self._applied_fills.add(oid)
        idx = self._grid_tag_line(order)
        if idx is None or idx < 0 or idx >= len(state["lines"]):
            return
        if order.ft_order_side == "buy":
            state["filled"].add(idx)
            self._save_grid_state()
        elif order.ft_order_side == "sell":
            state["filled"].discard(idx)
            state["refills"][idx] = state["refills"].get(idx, 0) + 1
            self._save_grid_state()

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime,
                    current_rate: float, current_profit: float,
                    **kwargs):
        state = self._grid_state(pair)
        if state is None or not self._finite_pos(current_rate):
            return None
        if current_rate > state["lines"][-1]:
            return "channel_top_exit"
        # downside break: below the channel bottom by half a step (so a
        # bottom-line buy has room to breathe) = the traded range broke;
        # cut the position and block re-entry for a cooldown.
        bottom_break = state["lines"][0] * (1 - state["step_pct"] / 2 / 100.0)
        if current_rate < bottom_break:
            self._recenter_block_until[pair] = current_time + \
                _tf_timedelta(self.timeframe,
                              self.recenter_cooldown_candles)
            return "channel_bottom_exit"
        return None

    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float,
                 entry_tag: str | None, side: str, **kwargs) -> float:
        return 1.0