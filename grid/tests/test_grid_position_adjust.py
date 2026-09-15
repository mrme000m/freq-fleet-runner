#!/usr/bin/env python3
"""Confirm-then-mutate position adjustment — the 2026-09-15 desync fix.

What the live dry-run fleet exposed (4 slots, ~8h, one trade per pair):
  * freqtrade vetoed 8 decided sells ("Remaining amount of ~10 would be
    smaller than the minimum of 14") AFTER the strategy had already
    marked the line sold — phantom refill counts, double inventory and
    orphaned lots with no line exit followed.
  * a canceled-unfilled grid buy still counted as "held"; the next
    candle the strategy SOLD never-bought inventory at that line's TP.
  * a TP order that rested 60m and timed out unfilled left its line
    marked sold — again orphaned inventory.

Contract tested here:
  * adjust_trade_position NEVER mutates filled/refills (decision only);
    line state moves solely in order_filled, idempotently per order_id.
  * a decided sell freqtrade would veto (remaining-position minimum) is
    never returned; the line stays filled and retries later.
  * lines with a resting (open) grid order are never re-decided, and
    pending buys occupy the max_buys budget and the max_stake room.
  * every buy window (fresh line and refill alike) is capped where the
    line's TP still clears the round-trip cost — the near-TP-edge buys
    that realized fee-negative churn are rejected.

The strategy module is imported directly with freqtrade/talib stubbed
(same pattern as test_lower_tf_rescale.py).
"""
import importlib.util
import os
import sys
import types
from datetime import datetime

import pytest

pd = pytest.importorskip("pandas")

HERE = os.path.dirname(os.path.abspath(__file__))
GRID = os.path.dirname(HERE)
STRAT_DIR = os.path.join(GRID, "strategies")
PAIR = "BTC/USDC:USDC"
DT = datetime(2026, 9, 15, 12, 0)


class _Param:
    """freqtrade DecimalParameter stub — .value pins to the default."""

    def __init__(self, low, high, default=None, decimals=None, space=None,
                 **kwargs):
        self.value = default


class _FakeIStrategy:
    def __init__(self, config=None):
        self.config = config or {}


class _Order:
    """Minimal freqtrade Order stand-in."""

    def __init__(self, tag, side, open_=False, oid="o"):
        self.ft_order_tag = tag
        self.ft_order_side = side
        self.ft_is_open = open_
        self.order_id = oid


class _Trade:
    """Minimal freqtrade Trade stand-in for the adjust/order callbacks."""

    def __init__(self, pair, stake_amount, amount, orders=None):
        self.pair = pair
        self.stake_amount = stake_amount
        self.amount = amount
        self.orders = orders or []


def _stub_freq_modules():
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
    ta.ATR = lambda df, timeperiod=14: df["high"] - df["low"]
    ta.EMA = lambda df, timeperiod=12: df["close"].ewm(span=timeperiod,
                                                      adjust=False).mean()
    sys.modules["freqtrade"] = pkg
    sys.modules["freqtrade.persistence"] = pers
    sys.modules["freqtrade.strategy"] = strat
    sys.modules["talib"] = talib
    sys.modules["talib.abstract"] = ta


@pytest.fixture(scope="module")
def gs():
    _stub_freq_modules()
    spec = importlib.util.spec_from_file_location(
        "GridStrategy_padjust", os.path.join(STRAT_DIR, "GridStrategy.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, STRAT_DIR)  # vendored grid_geometry import
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(STRAT_DIR)
    return mod


@pytest.fixture()
def s(gs):
    """Strategy instance with a fresh grid: price 100, atr_pct 0.4 ->
    step 0.4% (above the 0.24% taker fee floor), band_atr 3.0."""
    strat = gs.GridStrategy({})
    state = strat._build_grid(PAIR, 100.0, 0.4)
    return strat, state


def _adjust(strat, trade, entry_rate, exit_rate, min_stake=None,
            max_stake=1e9):
    return strat.adjust_trade_position(
        trade, DT, entry_rate, 0.0,
        min_stake, max_stake, entry_rate, exit_rate, 0.0, 0.0)


def _tp(state, j):
    return state["lines"][j] * (1 + state["step_pct"] / 100.0)


# --- decision-time purity + confirmation -------------------------------

def test_sell_decision_is_pure_and_confirmed_on_fill(s):
    strat, state = s
    j = len(state["lines"]) // 2
    state["filled"].add(j)
    per_line = strat._per_line_stake(state)
    trade = _Trade(PAIR, 5 * per_line, 5 * per_line / state["lines"][j])
    res = _adjust(strat, trade, _tp(state, j), _tp(state, j),
                  max_stake=per_line)
    assert res == (-per_line, f"grid_sell_L{j}")
    # decision did NOT touch the line state
    assert j in state["filled"]
    assert state["refills"] == {}
    # confirmation applies it — exactly once per order
    order = _Order(f"grid_sell_L{j}", "sell", oid="ord-1")
    strat.order_filled(PAIR, trade, order, DT)
    assert j not in state["filled"]
    assert state["refills"][j] == 1
    strat.order_filled(PAIR, trade, order, DT)
    assert state["refills"][j] == 1


def test_buy_decision_is_pure_and_confirmed_on_fill(s):
    strat, state = s
    per_line = strat._per_line_stake(state)
    ln = state["lines"][1]
    head = state["step_pct"] - strat._cost_floor()
    price = ln * (1 + head / 2 / 100.0)
    trade = _Trade(PAIR, per_line, per_line / price)
    res = _adjust(strat, trade, price, price * 0.99)
    assert res == (per_line, "grid_buy_L1")
    assert 1 not in state["filled"]
    strat.order_filled(PAIR, trade, _Order("grid_buy_L1", "buy", oid="b1"), DT)
    assert 1 in state["filled"]


# --- the min-stake veto must not leak a decision -------------------------

def test_vetoed_sell_keeps_line_filled_and_retries_when_viable(s):
    strat, state = s
    j = len(state["lines"]) // 2
    state["filled"].add(j)
    per_line = strat._per_line_stake(state)
    tp = _tp(state, j)
    # 2 lots: remaining after a one-line exit is ~1 lot, below the
    # exit-side minimum min_stake / (1-|stoploss|) * 1.02 (16.32 here,
    # vs a ~14.3 remaining) — freqtrade would veto the reduce
    trade = _Trade(PAIR, 2 * per_line, 2 * per_line / state["lines"][j])
    res = _adjust(strat, trade, tp, tp, min_stake=12.0, max_stake=per_line)
    assert res is None
    assert j in state["filled"]  # the desync regression: line STAYS held
    # same decision goes through once the position has grown
    trade.stake_amount = 5 * per_line
    trade.amount = 5 * per_line / state["lines"][j]
    res = _adjust(strat, trade, tp, tp, min_stake=12.0, max_stake=per_line)
    assert res == (-per_line, f"grid_sell_L{j}")


# --- pending (resting) grid orders --------------------------------------

def test_resting_sell_order_not_re_decided(s):
    strat, state = s
    j = len(state["lines"]) // 2
    state["filled"].add(j)
    per_line = strat._per_line_stake(state)
    order = _Order(f"grid_sell_L{j}", "sell", open_=True, oid="rest-sell")
    trade = _Trade(PAIR, 5 * per_line, 5 * per_line / state["lines"][j],
                   orders=[order])
    res = _adjust(strat, trade, _tp(state, j), _tp(state, j),
                  max_stake=per_line)
    assert res is None  # no double sell against one resting order
    assert j in state["filled"]


def test_resting_buy_order_not_re_decided(s):
    strat, state = s
    per_line = strat._per_line_stake(state)
    ln = state["lines"][1]
    head = state["step_pct"] - strat._cost_floor()
    price = ln * (1 + head / 2 / 100.0)
    order = _Order("grid_buy_L1", "buy", open_=True, oid="rest-buy")
    trade = _Trade(PAIR, per_line, per_line / price, orders=[order])
    res = _adjust(strat, trade, price, price * 0.99)
    assert res is None  # the resting buy already works line 1


def test_unfilled_line_cannot_be_sold(s):
    """The 2026-09-15 BTC case: line's buy order rests unfilled, price
    pops above the line's TP — the strategy must NOT sell inventory it
    never bought on that line."""
    strat, state = s
    per_line = strat._per_line_stake(state)
    order = _Order("grid_buy_L1", "buy", open_=True, oid="rest-buy")
    trade = _Trade(PAIR, per_line, per_line / state["lines"][1],
                   orders=[order])
    res = _adjust(strat, trade, _tp(state, 1) * 1.001, _tp(state, 1) * 1.001)
    assert res is None or res[1].startswith("grid_buy_L")
    assert 1 not in state["filled"]
    assert state["refills"] == {}


def test_pending_buy_counts_toward_max_buys(s):
    strat, state = s
    per_line = strat._per_line_stake(state)
    mb = strat._lines_below_mid(state)
    assert mb >= 3
    lower = [i for i, ln in enumerate(state["lines"])
             if ln < (state["low"] + state["high"]) / 2]
    free = lower[-1]
    for i in lower:
        if i != free:
            state["filled"].add(i)
    head = state["step_pct"] - strat._cost_floor()
    price = state["lines"][free] * (1 + head / 2 / 100.0)
    order = _Order(f"grid_buy_L{free}", "buy", open_=True, oid="pb")
    trade = _Trade(PAIR, per_line * mb, per_line * mb / price,
                   orders=[order])
    assert len(state["filled"]) == mb - 1
    res = _adjust(strat, trade, price, price * 0.99)
    assert res is None  # the pending buy occupies the last below-mid slot


# --- fee-viable buy windows ----------------------------------------------

def test_buy_window_is_fee_viable(s):
    strat, state = s
    per_line = strat._per_line_stake(state)
    ln = state["lines"][1]
    head = state["step_pct"] - strat._cost_floor()
    assert 0.0 < head < state["step_pct"]
    trade = _Trade(PAIR, per_line, per_line / ln)
    # near the TP edge (headroom beyond the fee-viable cap): rejected —
    # that fill would harvest less than the round-trip cost
    res = _adjust(strat, trade, ln * (1 + (head + 0.02) / 100.0),
                  ln * 0.99)
    assert res is None
    # inside the window: buys the line, TP clears the cost from the edge
    res = _adjust(strat, trade, ln * (1 + head / 2 / 100.0), ln * 0.99)
    assert res == (per_line, "grid_buy_L1")


# --- order_filled hygiene --------------------------------------------------

def test_order_filled_ignores_non_grid_and_out_of_range(s):
    strat, state = s
    j = len(state["lines"]) // 2
    state["filled"].add(j)
    per_line = strat._per_line_stake(state)
    trade = _Trade(PAIR, per_line, per_line / state["lines"][j])
    before = (set(state["filled"]), dict(state["refills"]))
    for k, tag in enumerate(["grid_recenter", None, "channel_top_exit",
                             f"grid_buy_L{len(state['lines']) + 5}"]):
        strat.order_filled(PAIR, trade, _Order(tag, "buy", oid=f"x{k}"), DT)
    assert (set(state["filled"]), dict(state["refills"])) == before
