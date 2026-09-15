#!/usr/bin/env python3
"""Confirm-then-mutate position adjustment + resting-ladder harvest.

What the live dry-run fleet exposed (4 slots, ~8h, one trade per pair):
  * freqtrade vetoed 8 decided sells ("Remaining amount of ~10 would be
    smaller than the minimum of 14") AFTER the strategy had already
    marked the line sold — phantom refill counts, double inventory and
    orphaned lots with no line exit followed.
  * a canceled-unfilled grid buy still counted as "held"; the next
    candle the strategy SOLD never-bought inventory at that line's TP.
  * a TP order that rested 60m and timed out unfilled left its line
    marked sold — again orphaned inventory.

And what the 2026-09-15 profitability audit exposed:
  * window-priced buys filled at the fee-viable window's TOP edge in a
    falling tape, harvesting ~= the round-trip cost (+0.25% gross vs
    0.24% cost) — fee-positive churn. Orders are now priced AT the
    line (buys) / AT the TP (sells) so every trip banks the full step.

Contract tested here:
  * adjust_trade_position NEVER mutates filled/refills (decision only);
    line state moves solely in order_filled, idempotently per order_id.
  * a decided sell freqtrade would veto (remaining-position minimum) is
    never returned; the line stays filled, and the loop FALLS THROUGH
    to the buy branch so the position can grow past the veto bar (the
    pre-fall-through shape deadlocked: vetoed sell re-decided every
    loop, buys never fired again).
  * lines with a resting (open) grid order are never re-decided, and
    pending buys occupy the max_buys budget and the max_stake room.
  * grid buys rest AT their line (custom_entry_price parses the tag),
    grid sells rest AT the TP (custom_exit_price parses the tag); the
    step >= min_step_multiple x cost entry gate is the per-trip fee bar.
  * placement is clamp-safe: orders only land on lines within
    _CUSTOM_PRICE_REACH of the current rate, so freqtrade's
    get_valid_price 2% clamp can never reprice a TP below its line's
    cost or a deep rung above its line.

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
    # price just above line 1 (inside the half-step marketable envelope)
    price = ln * (1 + state["step_pct"] / 4 / 100.0)
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
    # vs a ~14.3 remaining) — freqtrade would veto the reduce. With the
    # rate AT the top filled line's TP the strategy takes the flatten
    # escape hatch instead (whole position out, every lot fee-positive);
    # the line state is untouched until the fill confirms.
    trade = _Trade(PAIR, 2 * per_line, 2 * per_line / state["lines"][j])
    res = _adjust(strat, trade, tp, tp, min_stake=12.0)
    assert res == (-2 * per_line, f"grid_flatten_L{j}")
    assert j in state["filled"]  # the desync regression: line STAYS held
    # the fall-through when the flatten is NOT placeable (price fell
    # below the top filled line): the buy branch still works — the
    # pre-fall-through shape deadlocked on the re-decided sell.
    low = state["lines"][j] * (1 - state["step_pct"] / 2 / 100.0) * 0.999
    res = _adjust(strat, trade, low, low, min_stake=12.0)
    assert res is not None and res[1].startswith("grid_buy_L")
    assert j in state["filled"]
    # budget-full + nothing sellable -> genuinely nothing to do
    for i, ln in enumerate(state["lines"]):
        if ln < (state["low"] + state["high"]) / 2:
            state["filled"].add(i)
    res = _adjust(strat, trade, low, low, min_stake=12.0)
    assert res is None
    assert j in state["filled"]
    # a viable per-line sell goes through once the position has grown
    # (here via the trade object's stake directly — the fall-through
    # path above is how the live bot gets there). Lowest line first.
    trade.stake_amount = 5 * per_line
    trade.amount = 5 * per_line / state["lines"][j]
    res = _adjust(strat, trade, tp, tp, min_stake=12.0, max_stake=per_line)
    lowest = min(i for i, ln in enumerate(state["lines"])
                 if ln < (state["low"] + state["high"]) / 2)
    assert res == (-per_line, f"grid_sell_L{lowest}")


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
    price = ln * (1 + state["step_pct"] / 4 / 100.0)
    order = _Order("grid_buy_L1", "buy", open_=True, oid="rest-buy")
    trade = _Trade(PAIR, per_line, per_line / price, orders=[order])
    res = _adjust(strat, trade, price, price * 0.99)
    # line 1 already has its resting buy — never doubled. A buy on the
    # NEXT rung down (ladder building) is legitimate.
    assert res is None or res[1] != "grid_buy_L1"
    assert res is None or res[1].startswith("grid_buy_L")


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
    # resting buys on every below-mid line: the budget is fully occupied
    # by pending orders alone (nothing filled yet), so no new buy fires.
    orders = [_Order(f"grid_buy_L{i}", "buy", open_=True, oid=f"pb{i}")
              for i in lower]
    price = state["lines"][lower[-1]] * (1 + state["step_pct"] / 4 / 100.0)
    trade = _Trade(PAIR, per_line * mb, per_line * mb / price,
                   orders=orders)
    res = _adjust(strat, trade, price, price * 0.99)
    assert res is None  # pending buys occupy every below-mid slot


# --- resting-ladder harvest (the 2026-09-15 profitability pass) ---------

def test_grid_buy_is_priced_at_the_line(s):
    """custom_entry_price on a pos-adjust buy returns the LINE price, not
    the proposed market rate — the fill banks the full step to TP instead
    of the window position (the fee-churn regression: window-top fills
    harvested ~= the 0.24% cost floor)."""
    strat, state = s
    trade = _Trade(PAIR, 10.0, 0.1)
    for i, ln in enumerate(state["lines"][:3]):
        px = strat.custom_entry_price(PAIR, trade, DT, ln * 1.005,
                                      f"grid_buy_L{i}", "long")
        assert px == ln
    # non-grid / unknown tags keep the proposed rate
    assert strat.custom_entry_price(PAIR, trade, DT, 99.0,
                                    "grid_recenter", "long") == 99.0
    assert strat.custom_entry_price(PAIR, trade, DT, 99.0,
                                    None, "long") == 99.0


def test_grid_sell_is_priced_at_the_tp(s):
    """custom_exit_price on a grid_sell_Lj returns lines[j]*(1+step):
    the resting sell fills exactly at the TP touch."""
    strat, state = s
    trade = _Trade(PAIR, 10.0, 0.1)
    for j, ln in enumerate(state["lines"][:3]):
        px = strat.custom_exit_price(PAIR, trade, DT, ln, 0.0,
                                     f"grid_sell_L{j}")
        assert px == pytest.approx(_tp(state, j))
    # full exits / stoploss carry no L-tag -> proposed rate untouched
    assert strat.custom_exit_price(PAIR, trade, DT, 101.0, 0.05,
                                   "channel_top_exit") == 101.0
    assert strat.custom_exit_price(PAIR, trade, DT, 95.0, -0.1,
                                   "stoploss") == 95.0


def test_tp_sell_rests_before_price_reaches_it(s):
    """The sell is placed as soon as the line is filled — it does NOT
    wait for a loop where rate >= TP. A spike through the TP between
    loops can no longer slip past unharvested."""
    strat, state = s
    j = len(state["lines"]) // 2
    state["filled"].add(j)
    per_line = strat._per_line_stake(state)
    trade = _Trade(PAIR, 5 * per_line, 5 * per_line / state["lines"][j])
    rate = state["lines"][j] * 1.0005  # barely above the line, far < TP
    assert rate < _tp(state, j)
    res = _adjust(strat, trade, rate, rate, max_stake=per_line)
    assert res == (-per_line, f"grid_sell_L{j}")


def test_tp_beyond_clamp_envelope_withholds_sell(s):
    """Wide-step grid (HYPE's 2.15% live grid was one): with step 2.0%
    the TP sits 2% above the line — right at/beyond get_valid_price's
    2% clamp when the rate is still ~at the line, so a placed sell
    would be repriced toward market (potentially below cost over the
    round trip). The sell waits for the rate to rise into the envelope.
    The clamp guard is what remains once the rate >= line rule is met."""
    strat, _ = s
    state = strat._build_grid(PAIR, 100.0, 2.5)   # step clamps to 2.0%
    assert state["step_pct"] == pytest.approx(2.0)
    j = len(state["lines"]) // 2
    state["filled"].add(j)
    per_line = strat._per_line_stake(state)
    trade = _Trade(PAIR, 5 * per_line, 5 * per_line / state["lines"][j])
    ln = state["lines"][j]
    # rate AT the line: tp/rate = 1.02 > 1 + reach (1.019) -> withheld
    res = _adjust(strat, trade, ln, ln, max_stake=1e9)
    assert res is None or res[1].startswith("grid_buy_L")
    # rate 0.5% above the line: tp/rate = 1.0149 < 1.019 -> placed
    near = ln * 1.005
    res = _adjust(strat, trade, near, near, max_stake=per_line)
    assert res == (-per_line, f"grid_sell_L{j}")


def test_buy_ladder_is_reach_bounded(s):
    """Rungs deeper than _CUSTOM_PRICE_REACH below the rate get no order
    yet (get_valid_price would clamp them toward market); the ladder
    slides down with price. Rungs up to half a step ABOVE the rate fill
    immediately at their line when price gapped through between loops."""
    strat, state = s
    per_line = strat._per_line_stake(state)
    rate = (state["low"] + state["high"]) / 2
    trade = _Trade(PAIR, per_line, per_line / rate)
    res = _adjust(strat, trade, rate, rate)
    assert res is not None and res[1].startswith("grid_buy_L")
    i = int(res[1].rsplit("L", 1)[1])
    ln = state["lines"][i]
    assert rate * (1 - strat._CUSTOM_PRICE_REACH) <= ln < rate
    # every line in reach filled (with its resting TP sell already
    # working) -> deeper rungs stay untouched, no new order at all
    orders = []
    for k, ln2 in enumerate(state["lines"]):
        if ln2 < rate:
            state["filled"].add(k)
            orders.append(_Order(f"grid_sell_L{k}", "sell", open_=True,
                                 oid=f"ps{k}"))
    trade = _Trade(PAIR, per_line * len(orders),
                   per_line * len(orders) / rate, orders=orders)
    res = _adjust(strat, trade, rate, rate)
    assert res is None
    # gap-through: price half a step below a line still buys AT the line
    strat2 = strat  # same instance, fresh grid
    state["filled"].clear()
    gapped = state["lines"][4] * (1 - state["step_pct"] / 2 / 100.0
                                  ) * 1.0001
    trade2 = _Trade(PAIR, per_line, per_line / gapped)
    res = _adjust(strat2, trade2, gapped, gapped)
    assert res == (per_line, "grid_buy_L4")


def test_degenerate_grid_never_churns(s):
    """step <= round-trip cost (params edited mid-trade into the fee
    floor) -> no buys, no matter where price sits."""
    strat, state = s
    state["step_pct"] = strat._cost_floor() * 0.9
    per_line = strat._per_line_stake(state)
    rate = state["lines"][2]
    trade = _Trade(PAIR, per_line, per_line / rate)
    assert _adjust(strat, trade, rate, rate) is None


# --- single-order discipline (freqtrade REPLACE-cancel model) ------------

def test_parked_sell_never_pingpongs(s):
    """The 2026-09-15 BTC live failure: filled lines L4+L5, and the loop
    placed L5's sell (cancelling L4's), then L4's (cancelling L5's) —
    a REPLACE ping-pong every ~36s with zero fills. With one resting
    order and lowest-line-first selection, the parked sell on the
    lowest clamp-safe line must simply STAY parked."""
    strat, state = s
    j = len(state["lines"]) // 2
    state["filled"].update((j, j + 1))
    per_line = strat._per_line_stake(state)
    parked = _Order(f"grid_sell_L{j}", "sell", open_=True, oid="parked")
    rate = _tp(state, j) * 1.001  # both TPs clamp-safe, rate above lines
    trade = _Trade(PAIR, 5 * per_line, 5 * per_line / state["lines"][j],
                   orders=[parked])
    res = _adjust(strat, trade, rate, rate, max_stake=per_line)
    assert res is None or res[1] == f"grid_sell_L{j}" or \
        res[1].startswith("grid_buy_L")
    # and it must never decide the OTHER line's sell while j rests
    assert res != (-per_line, f"grid_sell_L{j + 1}")


def test_falling_tape_swaps_parked_sell_for_buy(s):
    """A TP sell parked above must not freeze the grid in a falling
    tape: once the rate drops below the parked sell's line, the ladder
    keeps buying down (placing the buy replaces the stale sell via
    freqtrade's REPLACE). Above the line, the sell stays parked and no
    buy fires (harvest outranks laddering)."""
    strat, state = s
    j = len(state["lines"]) // 2
    state["filled"].add(j)
    per_line = strat._per_line_stake(state)
    parked = _Order(f"grid_sell_L{j}", "sell", open_=True, oid="parked")
    # rate still at/above the parked line -> sell keeps working
    trade = _Trade(PAIR, 5 * per_line, 5 * per_line / state["lines"][j],
                   orders=[parked])
    rate = state["lines"][j] * 1.001
    res = _adjust(strat, trade, rate, rate, max_stake=1e9)
    assert res is None
    # rate below the line -> a buy may replace the stale parked sell
    low = state["lines"][j] * (1 - state["step_pct"] / 2 / 100.0) * 0.999
    res = _adjust(strat, trade, low, low, max_stake=1e9)
    assert res is not None and res[1].startswith("grid_buy_L")


def test_resting_buy_blocks_rung_hopping(s):
    """A resting buy is never re-decided and never swapped for a
    different rung — re-placing would just REPLACE-cancel/rebook the
    ladder's leading edge every loop."""
    strat, state = s
    per_line = strat._per_line_stake(state)
    ln = state["lines"][2]
    order = _Order("grid_buy_L2", "buy", open_=True, oid="rest-buy")
    rate = ln * 1.001
    trade = _Trade(PAIR, per_line, per_line / rate, orders=[order])
    assert _adjust(strat, trade, rate, rate) is None


# --- min-exit veto -> full-position flatten (the 2-lot trap) --------------

def test_vetoed_sell_flattens_at_top_tp(s):
    """freqtrade vetoes a one-line reduce whose remainder falls below
    min_stake/(1-|stoploss|): a 2-lot position at ~min_stake per lot can
    never partially exit, and with capital-bounded ladder depth the buy
    branch may be budget-full too — the position would park until the
    channel exits. The escape hatch: flatten the WHOLE position at the
    HIGHEST filled line's TP (every lot banks >= its line -> top TP,
    structurally fee-positive), tagged grid_flatten_Li so the M4 ledger
    reads it as the trade's full exit (not a per-line trip)."""
    strat, state = s
    lo, hi = state["lines"][3], state["lines"][4]
    state["filled"].update((3, 4))
    per_line = strat._per_line_stake(state)
    # 2 lots: remaining after a one-lot reduce ~ per_line < bar (16.32)
    trade = _Trade(PAIR, 2 * per_line, 2 * per_line / lo)
    rate = hi * 1.001  # at/above the top line, its TP clamp-safe
    res = _adjust(strat, trade, rate, rate, min_stake=12.0)
    assert res == (-2 * per_line, "grid_flatten_L4")
    # the flatten prices at the top line's TP
    px = strat.custom_exit_price(PAIR, trade, DT, rate, 0.0, res[1])
    assert px == pytest.approx(_tp(state, 4))
    # the ledger's per-line regex must NOT match it (pairing treats it
    # as the full exit — one full_exit trip per lot)
    import re as _re
    assert not _re.search(r"grid_(buy|sell)_L(\d+)$", res[1])
    assert _re.match(r"grid_flatten_L(\d+)$", res[1])
    # and the tag leaves line state untouched in order_filled (the trade
    # closes; the next entry rebuilds the grid)
    before = (set(state["filled"]), dict(state["refills"]))
    strat.order_filled(PAIR, trade, _Order(res[1], "sell", oid="fl1"), DT)
    assert (set(state["filled"]), dict(state["refills"])) == before


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
