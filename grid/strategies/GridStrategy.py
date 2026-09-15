"""GridStrategy — single-position DCA grid spike (M1).

Single open trade per pair. On entry (tag `grid_recenter`) the strategy
builds an ATR-channel geometric grid from the shared geometry module and
buys/sells per grid line via adjust_trade_position (tags grid_buy_Li /
grid_sell_Lj). Full profit exit at the channel top (channel_top_exit).

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
import sys
from datetime import datetime

try:
    from grid_geometry import (  # noqa: E402
        channel,
        closest_levels,
        fee_floor_step,
        geometric_lines,
        per_line_size,
    )
except ImportError:
    sys.path.insert(0, "/Volumes/ExMac/code/tradingview/go/agents/grid-autonomy")

    from execution.grid_geometry import (  # noqa: E402
        channel,
        closest_levels,
        fee_floor_step,
        geometric_lines,
        per_line_size,
    )

from freqtrade.persistence import Trade
from freqtrade.strategy import DecimalParameter, IStrategy

import talib.abstract as ta


class GridStrategy(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    startup_candle_count = 30
    process_only_new_candles = True
    can_short = False

    # --- geometry knobs (hyperoptable, space=buy) ---
    band_atr = DecimalParameter(1.5, 5.0, default=3.0, decimals=1, space="buy")
    # step derives from ATR like the daemon: step = min(step_max,
    # max(step_min, atr_pct * step_factor)), then the fee floor inside
    # fee_floor_step still binds (structural invariant, not tuned away).
    step_factor = DecimalParameter(0.2, 1.0, default=0.5, decimals=2,
                                   space="buy")
    # --- fixed sizing / venue knobs (not hyperopted) ---
    alloc_usd = 100.0
    min_cost = 10.0
    venue = "hyperliquid"
    spread_pct = 0.02
    step_min = 0.1  # grid_defaults parity (fee_floor_step defaults)
    step_max = 2.0

    minimal_roi = {"0": 100}
    stoploss = -0.25
    position_adjustment_enable = True
    max_entry_position_adjustment = -1

    def __init__(self, config=None):
        super().__init__(config)
        self._grids: dict = {}

    # --- helpers -----------------------------------------------------

    def _build_grid(self, pair: str, price: float, atr_pct: float) -> dict:
        """Compute channel + lines and store grid state for `pair`."""
        raw_step_pct = atr_pct * self.step_factor.value
        s = fee_floor_step(raw_step_pct, self.spread_pct, self.venue,
                           step_min=self.step_min, step_max=self.step_max)
        low, high = channel(price, atr_pct, band_atr=self.band_atr.value)
        lines = geometric_lines(low, high, s)
        state = {
            "lines": lines,
            "step_pct": s,
            "low": low,
            "high": high,
            "filled": set(),
            "pending_sell": set(),
        }
        self._grids[pair] = state
        return state

    def _grid_state(self, pair: str):
        return self._grids.get(pair)

    def _per_line_stake(self, state: dict) -> float:
        return per_line_size(self.alloc_usd, len(state["lines"]), self.min_cost)

    def _lines_below_mid(self, state: dict) -> int:
        mid = (state["low"] + state["high"]) / 2.0
        return sum(1 for ln in state["lines"] if ln < mid)

    # --- freqtrade interface ----------------------------------------

    def populate_indicators(self, dataframe, metadata):
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"] * 100
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        dataframe.loc[dataframe["volume"] > 0, ["enter_long", "enter_tag"]] = (
            1,
            "grid_recenter",
        )
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        return dataframe

    def custom_entry_price(self, pair: str, trade, current_time: datetime,
                           proposed_rate: float, entry_tag: str | None,
                           side: str, **kwargs) -> float:
        if trade is not None:
            # Position-adjustment order (grid buy): keep the existing grid
            # state — rebuilding here would wipe filled/pending sets.
            return proposed_rate
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        last = dataframe.iloc[-1]
        state = self._build_grid(pair, float(last["close"]),
                                 float(last["atr_pct"]))
        line, _above = closest_levels(state["lines"], proposed_rate)
        return line

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
            last = dataframe.iloc[-1]
            atr_pct = float(last["atr_pct"]) if last["atr_pct"] else 0.0
            state = self._build_grid(pair, float(last["close"]), atr_pct)
        stake = self._per_line_stake(state)
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
        state = self._grid_state(trade.pair)
        if state is None:
            return None
        lines = state["lines"]
        filled = state["filled"]
        pending = state["pending_sell"]
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

        # --- GRID SELL (checked first) ---
        # highest filled line whose TP is reached
        best_j = None
        for j, ln in enumerate(lines):
            if j in filled and j not in pending:
                if current_exit_rate >= ln * (1 + step / 100.0):
                    if best_j is None or lines[best_j] < ln:
                        best_j = j
        if best_j is not None:
            filled.discard(best_j)
            pending.add(best_j)
            stake = -min(per_line, max_stake)
            return (stake, f"grid_sell_L{best_j}")

        # --- GRID BUY ---
        max_buys = self._lines_below_mid(state)
        if len(filled) >= max_buys:
            return None
        if current_entry_rate > lines[-1]:
            # price above the channel top: channel_top_exit fires this very
            # candle, so buying now would be exited flat in the same candle
            return None
        # highest line whose buy window contains the current price:
        # line < rate < line*(1+step). Buying only inside the window
        # guarantees the later TP sell fills above the buy fill; without
        # it the strategy buys stale low lines (whose TP is already met)
        # and instantly sells them at or below the buy fill.
        best_i = None
        for i, ln in enumerate(lines):
            tp = ln * (1 + step / 100.0)
            if ln < current_entry_rate < tp and i not in filled and i not in pending:
                if best_i is None or lines[best_i] < ln:
                    best_i = i
        if best_i is None:
            return None
        new_total = trade.stake_amount + per_line
        if max_stake and new_total > max_stake:
            return None
        filled.add(best_i)
        return (per_line, f"grid_buy_L{best_i}")

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime,
                    current_rate: float, current_profit: float,
                    **kwargs):
        state = self._grid_state(pair)
        if state is None:
            return None
        if current_rate > state["lines"][-1]:
            return "channel_top_exit"
        return None

    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float,
                 entry_tag: str | None, side: str, **kwargs) -> float:
        return 1.0
