#!/usr/bin/env python3
"""Dynamic TF rescaling — the ATR reference-horizon contract.

GridStrategy's tuned params (band_atr 4.2, min_atr_pct 0.3, the
min_step_multiple x cost entry gate) are denominated in 1h-horizon
volatility. On the 1m-5m band every ATR% the strategy consumes must be
renormalized by _atr_scale(timeframe) = sqrt(60 / tf_minutes) so a
lower-TF slot actually reaches the gates and builds a sane channel —
while a 1h slot stays bit-equal to the calibrated regime (factor 1.0).

These tests import the strategy module directly, stubbing the
freqtrade + talib imports (the suite is stdlib-only); pandas is used
for real dataframe math, matching the freqtrade stack.
"""
import importlib.util
import math
import os
import sys
import types

import pytest

pd = pytest.importorskip("pandas")

HERE = os.path.dirname(os.path.abspath(__file__))
GRID = os.path.dirname(HERE)
STRAT_DIR = os.path.join(GRID, "strategies")


# --- strategy module under test (freqtrade/talib stubbed) -----------------

class _Param:
    """freqtrade DecimalParameter stub — .value pins to the default."""

    def __init__(self, low, high, default=None, decimals=None, space=None,
                 **kwargs):
        self.value = default


class _FakeIStrategy:
    def __init__(self, config=None):
        self.config = config or {}


def _stub_freq_modules():
    """Register minimal freqtrade/talib modules so GridStrategy imports
    without the freqtrade runtime. freqtrade.enums.runmode is left
    UNstubbed on purpose: _persist_enabled's import fails -> returns
    False -> no grid_state.json side-files from these tests."""
    for name in ("freqtrade", "freqtrade.persistence", "freqtrade.strategy",
                 "talib", "talib.abstract"):
        sys.modules.pop(name, None)
    pkg = types.ModuleType("freqtrade")
    pers = types.ModuleType("freqtrade.persistence")
    pers.Trade = object
    strat = types.ModuleType("freqtrade.strategy")
    strat.DecimalParameter = _Param
    strat.IStrategy = _FakeIStrategy
    talib = types.ModuleType("talib")
    ta = types.ModuleType("talib.abstract")

    def _atr(df, timeperiod=14):
        # exact for the constant-wick candles below: every TR equals
        # high - low, so ATR(n) == high - low on every bar.
        return df["high"] - df["low"]

    def _ema(df, timeperiod=12):
        return df["close"].ewm(span=timeperiod, adjust=False).mean()

    ta.ATR = _atr
    ta.EMA = _ema
    sys.modules["freqtrade"] = pkg
    sys.modules["freqtrade.persistence"] = pers
    sys.modules["freqtrade.strategy"] = strat
    sys.modules["talib"] = talib
    sys.modules["talib.abstract"] = ta


@pytest.fixture(scope="module")
def gs():
    _stub_freq_modules()
    spec = importlib.util.spec_from_file_location(
        "GridStrategy_under_test", os.path.join(STRAT_DIR, "GridStrategy.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, STRAT_DIR)  # vendored grid_geometry import
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(STRAT_DIR)
    return mod


# --- pure helpers ----------------------------------------------------------

def test_atr_scale_factors(gs):
    """sqrt-of-time renormalization per slot TF of the 2026-09-15 reset;
    1h is EXACTLY 1.0 so the calibrated regime is unchanged."""
    assert gs._atr_scale("1m") == pytest.approx(math.sqrt(60.0))
    assert gs._atr_scale("3m") == pytest.approx(math.sqrt(20.0))
    assert gs._atr_scale("5m") == pytest.approx(math.sqrt(12.0))
    assert gs._atr_scale("1h") == 1.0
    assert gs._atr_scale("4h") == pytest.approx(0.5)
    assert gs._atr_scale("15m") == pytest.approx(2.0)


def test_tf_minutes(gs):
    assert gs._tf_minutes("1m") == 1.0
    assert gs._tf_minutes("5m") == 5.0
    assert gs._tf_minutes("1h") == 60.0
    assert gs._tf_minutes("4h") == 240.0


# --- trend-horizon rescaling ----------------------------------------------

def test_trend_ema_period_renormalizes_horizon(gs):
    """The tuned gate is EMA26 on 1h == a ~26h trend. Every slot TF must
    measure the SAME horizon in bars of its own size; 1h stays bit-equal
    (exactly 26) and the 1m-5m band scales up (1560 / 520 / 312)."""
    assert gs._trend_ema_period("1h") == 26
    assert gs._trend_ema_period("1m") == 1560
    assert gs._trend_ema_period("3m") == 520
    assert gs._trend_ema_period("5m") == 312
    assert gs._trend_ema_period("15m") == 104


def test_startup_candle_count_covers_trend_horizon(gs):
    """The EMA must be warm: startup covers the rescaled period (+40),
    so a 1m slot fetches ~1600 bars (~27h) and a 1h slot stays lean."""
    assert gs.GridStrategy({"timeframe": "1m"}).startup_candle_count == 1600
    assert gs.GridStrategy({"timeframe": "3m"}).startup_candle_count == 560
    assert gs.GridStrategy({"timeframe": "5m"}).startup_candle_count == 352
    assert gs.GridStrategy({"timeframe": "1h"}).startup_candle_count == 70


def test_entry_blocked_in_multiday_downtrend_despite_micro_bounce(gs):
    """THE HYPE regression (2026-09-15): price sat ~at the 26-bar micro
    EMA (130min on 5m) while >0.5% under the 26h trend — the pre-fix
    gate waved entries into a falling tape all session. With the
    rescaled horizon the same wall-clock tape is blocked on 5m AND on
    1h (the calibrated regime), by the TREND gate — vol clears on both.
    """
    # same wall-clock shape at both TFs: ~21h flat at 100, drop to 97,
    # ~8h flat. 5m: 252+96 bars; 1h: 21+8 bars.
    tapes = {"5m": [100.0] * 252 + [97.0] * 96,
             "1h": [100.0] * 21 + [97.0] * 8}
    for tf, closes in tapes.items():
        df = pd.DataFrame({
            "open": closes,
            "high": [c * 1.002 for c in closes],   # raw ATR% ~0.4%/bar:
            "low": [c * 0.998 for c in closes],    #   5m -> 1.39%, 1h -> 0.4%
            "close": closes, "volume": [10.0] * len(closes),
        })
        s = _strategy_at_tf(gs, tf)
        out = s.populate_indicators(df.copy(), None)
        # premise: vol gate passes (so the TREND gate is the blocker)
        last = out.iloc[-1]
        assert last["atr_pct"] >= s._entry_gate_atr(), tf
        if tf == "5m":
            # the pre-fix 26-bar EMA converges inside the 8h flat tail —
            # the old gate would have ENTERED here (the regression)
            micro = df["close"].ewm(span=26, adjust=False).mean().iloc[-1]
            assert df["close"].iloc[-1] >= micro * (1 - 0.5 / 100), tf
        out = s.populate_entry_trend(out, None)
        # the flat-at-100 segment legitimately enters; what must stay
        # blocked is every bar AFTER the drop into the downtrend
        n_flat_tail = 8 if tf == "1h" else 96
        tail = out["enter_long"].iloc[-n_flat_tail:]
        assert int((tail == 1).sum()) == 0, (
            f"{tf}: entries must stay blocked under the 26h trend")


def test_entry_gate_atr_default_params(gs):
    """With the shipped defaults the effective gate is the fee-clearance
    bar 1.5 x (2x0.02 spread + 0.20 taker round trip) = 0.36% — the
    1h-horizon ATR% floor the entry filter enforces."""
    s = gs.GridStrategy({})
    assert s._cost_floor() == pytest.approx(0.24)
    assert s._entry_gate_atr() == pytest.approx(0.36)


# --- indicator normalization + entry gate interaction ---------------------

def _candles(n=200, close=77665.0, wick_pct=0.03):
    """Constant-wick candles: every bar spans close*(1±wick_pct/100), so
    ATR = high-low and raw ATR% = 2*wick_pct on every bar (0.06% matches
    the observed BTC/1m mean from 2026-09-15)."""
    hi = close * (1 + wick_pct / 100)
    lo = close * (1 - wick_pct / 100)
    return pd.DataFrame({
        "open": [close] * n, "high": [hi] * n, "low": [lo] * n,
        "close": [close] * n, "volume": [10.0] * n,
    })


def _strategy_at_tf(gs, tf):
    s = gs.GridStrategy({})
    s.timeframe = tf
    return s


def test_populate_indicators_renormalizes_atr_pct(gs):
    """atr_pct is the 1h-equivalent ATR%: raw 0.06% at 1m becomes
    0.06*sqrt(60) ~ 0.465%; the raw column (atr in price units) is
    untouched."""
    s = _strategy_at_tf(gs, "1m")
    df = s.populate_indicators(_candles(), None)
    raw = (df["high"] - df["low"]) / df["close"] * 100
    assert ((raw - 0.06).abs() < 1e-9).all()
    expected = 0.06 * math.sqrt(60)
    assert ((df["atr_pct"] - expected).abs() < 1e-9).all()


def test_entry_fires_on_1m_but_not_1h(gs):
    """THE regression these tests guard: mean vol observed live on
    2026-09-15 (BTC/1m 0.06, ETH/3m 0.125, HYPE/5m 0.26 raw ATR%/bar)
    is fee-degenerate at the 1h horizon (0.06 < 0.3 floor, no entries —
    correct) but trades on the lower-TF band once renormalized to the
    reference horizon (0.465 / 0.559 / 0.90 >= the 0.36% gate)."""
    cases = [
        ("1m", 0.06, True),    # BTC slot mean -> 0.06*sqrt(60) = 0.465
        ("3m", 0.125, True),   # ETH slot mean -> 0.125*sqrt(20) = 0.559
        ("5m", 0.26, True),    # HYPE slot mean -> 0.26*sqrt(12) = 0.90
        ("1h", 0.06, False),   # same raw vol at 1h stays below the floor
    ]
    for tf, raw_vol, expect_entries in cases:
        s = _strategy_at_tf(gs, tf)
        df = s.populate_indicators(_candles(wick_pct=raw_vol / 2), None)
        df = s.populate_entry_trend(df, None)
        n = int((df["enter_long"] == 1).sum())
        if expect_entries:
            assert n > 0, f"{tf}: rescaled vol must clear the gate"
        else:
            assert n == 0, f"{tf}: raw vol below the floor must not enter"


def test_entry_still_blocks_thin_vol_at_1m(gs):
    """Rescaling does NOT loosen the fee economics: a dead-quiet 1m tape
    (raw 0.01%/bar -> 0.077% at the 1h horizon) stays below the 0.36%
    gate and never enters."""
    s = _strategy_at_tf(gs, "1m")
    df = s.populate_indicators(_candles(wick_pct=0.005), None)
    df = s.populate_entry_trend(df, None)
    assert int((df["enter_long"] == 1).sum()) == 0


# --- grid geometry at the reference horizon --------------------------------

def test_build_grid_fee_clearance_and_channel(gs):
    """A grid built from renormalized vol keeps the M5 invariants: the
    step clears the round-trip cost by >= min_step_multiple, and the
    channel is wide enough for a real ladder (not the +-0.25% pinhole
    raw 1m vol would give). No grid_state.json side-file is written
    (persist is disabled outside trade runmodes)."""
    s = _strategy_at_tf(gs, "1m")
    price = 77665.0
    atr_ref = 0.06 * math.sqrt(60)
    state = s._build_grid("BTC/USDC:USDC", price, atr_ref)

    step = state["step_pct"]
    assert step >= s.min_step_multiple.value * s._cost_floor()
    assert step == pytest.approx(atr_ref * s.step_factor.value)
    band = s.band_atr.value * atr_ref / 100
    assert state["low"] == pytest.approx(price * (1 - band))
    assert state["high"] == pytest.approx(price * (1 + band))
    # sane ladder density: several steps across the channel, not a
    # pinhole and not a degenerate 1-2 lines
    assert 3 <= len(state["lines"]) <= 50
    assert not os.path.exists(os.path.join(STRAT_DIR, "grid_state.json"))


def test_refhorizon_economics_match_1h_slot(gs):
    """The rescaling is a horizon transform, not a retune: a 1m slot at
    renormalized vol and a 1h slot at the same vol build the SAME grid
    (step, band, line count) — proving lower-TF slots inherit the
    calibrated 1h economics exactly."""
    price, atr_ref = 77665.0, 0.465
    grids = {}
    for tf in ("1m", "1h"):
        s = _strategy_at_tf(gs, tf)
        grids[tf] = s._build_grid("BTC/USDC:USDC", price, atr_ref)
    assert grids["1m"]["step_pct"] == pytest.approx(grids["1h"]["step_pct"])
    assert grids["1m"]["low"] == pytest.approx(grids["1h"]["low"])
    assert grids["1m"]["high"] == pytest.approx(grids["1h"]["high"])
    assert len(grids["1m"]["lines"]) == len(grids["1h"]["lines"])
