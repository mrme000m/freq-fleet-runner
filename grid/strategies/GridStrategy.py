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
  * bounded line refill (max_refills_per_line) with a tightened re-buy
    window so refills stay far enough below TP to clear fees.
  * grid state persists to <user_data_dir>/grid_state.json and is
    restored on bot_start so a restart mid-trade keeps its fills.
  * alloc_usd / min_cost / spread_pct are hyperoptable; alloc_usd scales
    by config["ladder_pct"] (injected by the deployer from the M4
    reliability ledger ladder).

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
import os
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

    # --- fixed economics / structural knobs ---
    venue = "hyperliquid"
    use_taker_fee = True  # config charges taker per side; floor must match
    step_min = 0.1  # grid_defaults parity (fee_floor_step defaults)
    step_max = 2.0
    max_refills_per_line = 2  # bounded re-buys of a sold line per trade
    # candles (1h) to wait before re-entering after a channel-bottom exit
    recenter_cooldown_candles = 6

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

    # --- helpers -----------------------------------------------------

    def _ladder_pct_cfg(self) -> float:
        """ladder_pct injected by the deployer (M4 ladder): 0.25/0.40/0.50
        scale alloc; 0.0 = kill gate (stop new entries); absent → 1.0."""
        raw = (self.config or {}).get("ladder_pct")
        try:
            return float(raw) if raw is not None else 1.0
        except (TypeError, ValueError):
            return 1.0

    def _alloc_usd(self) -> float:
        """Grid notional, scaled by the deployer's ladder_pct (M4 ledger
        ladder: base 0.25 / probe 0.40 / full 0.50), default 1.0."""
        return float(self.alloc_usd.value) * self._ladder_pct_cfg()

    def _cost_floor(self) -> float:
        """Round-trip cost the config actually charges, in PERCENT:
        2×spread + round-trip fee (taker by default)."""
        return 2 * float(self.spread_pct.value) + round_trip_fee_pct(
            self.venue, taker=self.use_taker_fee)

    def _build_grid(self, pair: str, price: float, atr_pct: float) -> dict:
        """Compute channel + lines and store grid state for `pair`."""
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
        return per_line_size(self._alloc_usd(), len(state["lines"]),
                             self.min_cost.value)

    def _lines_below_mid(self, state: dict) -> int:
        mid = (state["low"] + state["high"]) / 2.0
        return sum(1 for ln in state["lines"] if ln < mid)

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
                self._grids[pair] = state
            except (KeyError, TypeError, ValueError):
                continue

    # --- freqtrade interface ----------------------------------------

    def populate_indicators(self, dataframe, metadata):
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"] * 100
        dataframe["ema_short"] = ta.EMA(dataframe, timeperiod=12)
        dataframe["ema_long"] = ta.EMA(dataframe, timeperiod=26)
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        # volatility gate: the grid step must clear fees by a healthy
        # margin (min_step_multiple × cost) and clear an absolute ATR floor
        cost = self._cost_floor()
        req_atr = self.min_step_multiple.value * cost / \
            max(self.step_factor.value, 1e-6)
        vol_ok = dataframe["atr_pct"] >= \
            max(self.min_atr_pct.value, req_atr)
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
            # Position-adjustment order (grid buy): keep the existing grid
            # state — rebuilding here would wipe filled/refill sets.
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

        # --- GRID SELL (checked first) ---
        # highest filled line whose TP is reached
        best_j = None
        for j, ln in enumerate(lines):
            if j in filled:
                if current_exit_rate >= ln * (1 + step / 100.0):
                    if best_j is None or lines[best_j] < ln:
                        best_j = j
        if best_j is not None:
            filled.discard(best_j)
            refills[best_j] = refills.get(best_j, 0) + 1
            self._save_grid_state()
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
        # Highest line whose buy window contains the current price. Fresh
        # lines use the full window (line, tp); refilled lines use only the
        # lower half (line, line*(1+step/2)) so a re-buy sits far enough
        # below its TP to clear fees on the next sell (buying at the TP
        # edge is fee-negative churn). Bounded by max_refills_per_line.
        best_i = None
        for i, ln in enumerate(lines):
            if i in filled:
                continue
            n_refills = refills.get(i, 0)
            if n_refills >= self.max_refills_per_line:
                continue
            tp = ln * (1 + step / 100.0)
            upper = ln * (1 + step / 2 / 100.0) if n_refills else tp
            if ln < current_entry_rate < upper:
                if best_i is None or lines[best_i] < ln:
                    best_i = i
        if best_i is None:
            return None
        new_total = trade.stake_amount + per_line
        if max_stake and new_total > max_stake:
            return None
        filled.add(best_i)
        self._save_grid_state()
        return (per_line, f"grid_buy_L{best_i}")

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime,
                    current_rate: float, current_profit: float,
                    **kwargs):
        state = self._grid_state(pair)
        if state is None:
            return None
        if current_rate > state["lines"][-1]:
            return "channel_top_exit"
        # downside break: below the channel bottom by half a step (so a
        # bottom-line buy has room to breathe) = the traded range broke;
        # cut the position and block re-entry for a cooldown.
        bottom_break = state["lines"][0] * (1 - state["step_pct"] / 2 / 100.0)
        if current_rate < bottom_break:
            self._recenter_block_until[pair] = current_time + timedelta(
                hours=self.recenter_cooldown_candles)
            return "channel_bottom_exit"
        return None

    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float,
                 entry_tag: str | None, side: str, **kwargs) -> float:
        return 1.0