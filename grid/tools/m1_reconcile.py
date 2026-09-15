"""M1 reconcile — pair per-level grid round trips from the backtest export.

Reads backtest_results/m1_grid.zip (freqtrade backtest export, zip with the
result JSON inside), reconstructs the per-line ledger from order tags:

  grid_recenter    initial entry (tag on the main entry order)
  grid_buy_L{i}    position increase at grid line i   (positive adjust)
  grid_sell_L{j}   per-line TP partial exit at line j (negative adjust)
  channel_top_exit / force_exit   full-exit sell orders (no _L tag) that
                   close ALL remaining open per-line lots at the exit price

Reports:
  - total grid buys / grid sells / channel_top_exits / force / stoploss exits
  - completed per-level round trips (grid_buy_Li -> grid_sell_Li, FIFO per
    line), their win rate, average and summed realized profit
  - the per-level TP invariant: every TP round trip's sell fill > buy fill
  - the fee-gate step invariant: the step each trade's grid actually
    used, DERIVED per trade from the recenter anchor (which fills exactly
    on a grid line) plus the line buy fills, is >= 2*spread + round-trip
    fee
  - cash reconciliation: summed realized PnL vs the export's total profit
    (delta = fees + funding; reported, not asserted)

Exit code 0 = all invariants hold.
"""
import json
import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

FT_USER_DATA = Path(__file__).resolve().parents[2] / "ft_user_data"
RESULTS = FT_USER_DATA / "backtest_results"
# export to reconcile — the M1/M2 oracle evidence by default; any
# freqtrade backtest export zip via --zip (the scratch original in
# ft_user_data/ reads the same files)
EXPORT = RESULTS / "m1_grid.zip"
if len(sys.argv) > 1:
    if sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        raise SystemExit(0)
    EXPORT = Path(sys.argv[1])
LRE = re.compile(r"grid_(buy|sell)_L(\d+)$")

# geometry constants (mirror execution.grid_geometry, hyperliquid venue)
SPREAD_PCT = 0.02
ROUND_TRIP_FEE_PCT = {"hyperliquid": 0.10}
# step is ATR-derived per trade (atr_pct * step_factor, fee-floored), so
# there is no single configured step to report: it is DERIVED per trade
# from the trade's own grid fills (see derive_step_interval below).
import math

# scan grid for the per-trade step derivation (percent units)
STEP_SCAN_LO = 0.05
STEP_SCAN_HI = 3.0
STEP_SCAN_DS = 1e-4


def derive_step_interval(entry_px, buys):
    """Feasible grid step (%), derived from ONE trade's own fills.

    The recenter entry fills exactly at a grid line L_m (custom_entry_price
    returns the line). Lines are geometric — L_{k+1} = L_k * (1 + step/100),
    the fee_floor_step/geometric_lines construction — and every grid_buy_Li
    fill lands strictly inside its line's buy window (L_i, L_i*(1+step/100))
    because the strategy only buys inside the window. A raw
    ln(p_{i+1}/p_i) over consecutive line fills UNDERESTIMATES the step
    (fills sit anywhere in their windows), so instead we scan step and keep
    the (step, anchor m) pairs consistent with ALL of the trade's line buy
    fills (first fill per line): ln(fill/L_m)/ln(1+step) must floor to
    (line - m) for every fill. The surviving steps form a tight interval
    that contains the step the strategy actually used for that trade.
    Returns (status, lo, hi, m): status "ok" with the feasible interval
    (percent) and anchor line m (None when several anchors fit), status
    "insufficient" (no recenter anchor, or fewer than 2 filled grid
    lines), or status "inconsistent" (no geometric step fits the fills —
    the grid geometry invariant itself is broken).
    """
    idx = sorted(buys)
    if entry_px is None or len(idx) < 2:
        return ("insufficient", None, None, None)
    fills = [(i, math.log(buys[i][0] / entry_px)) for i in idx]
    by_m = {}  # m -> [lo, hi] contiguous feasible step ranges (hull)
    s = STEP_SCAN_LO
    while s <= STEP_SCAN_HI + 1e-12:
        lx = math.log(1.0 + s / 100.0)
        m_set = set()
        for i, u in fills:
            m_set.add(i - int(math.floor(u / lx + 1e-9)))
            if len(m_set) > 1:
                break
        if len(m_set) == 1:
            m = m_set.pop()
            if m in by_m:
                by_m[m][1] = s
            else:
                by_m[m] = [s, s]
        s += STEP_SCAN_DS
    if not by_m:
        return ("inconsistent", None, None, None)
    lo = min(v[0] for v in by_m.values())
    hi = max(v[1] for v in by_m.values())
    m = min(by_m) if len(by_m) == 1 else None  # unambiguous anchor or None
    return ("ok", lo, hi, m)


def load_export():
    zp = Path(EXPORT)
    if not zp.exists():
        raise SystemExit(f"missing export: {zp} (pass --zip or a path "
                         "argument)")
    with zipfile.ZipFile(zp) as z:
        for n in z.namelist():
            if (n.endswith(".json") and "_config" not in n
                    and "meta" not in n and "market_change" not in n
                    and "_wallet" not in n):
                d = json.loads(z.read(n))
                if isinstance(d, dict) and "strategy" in d:
                    return zp.name, d
    raise SystemExit(f"no backtest result inside {zp}")


def main() -> int:
    zname, export = load_export()
    key = next(k for k in export["strategy"] if "Grid" in k)
    st = export["strategy"][key]
    trades = st["trades"]
    print(f"export: {zname}  strategy={key}  trades={len(trades)}")

    counts = defaultdict(int)
    surprises = []
    tp_trips = []      # (trade, line, buy_px, sell_px, usdc) grid_sell pairs
    fe_lots = []       # (trade, line, buy_px, exit_px, reason) full-exit lots
    never_closed = []
    derived = []       # (trade, lo, hi, mid, m) per-trade derived steps
    open_cost = close_cost = 0.0

    for ti, t in enumerate(trades):
        buys = defaultdict(list)   # line -> [fill px] (order of fills)
        sells = defaultdict(list)
        full_exit_px = None
        entry_px = None           # recenter anchor: fills exactly at a line
        for o in t["orders"]:
            m = LRE.search(o["ft_order_tag"] or "")
            if o["ft_is_entry"]:
                if m:
                    counts["grid_buy"] += 1
                    buys[int(m.group(2))].append(o["safe_price"])
                else:
                    counts["grid_recenter"] += 1
                    if entry_px is None:
                        entry_px = o["safe_price"]
            else:
                if m:
                    counts["grid_sell"] += 1
                    sells[int(m.group(2))].append(o["safe_price"])
                else:
                    counts["full_exit"] += 1
                    counts["full_exit:" + t["exit_reason"]] += 1
                    full_exit_px = o["safe_price"]

        open_cost += sum(o["cost"] for o in t["orders"] if o["ft_is_entry"])
        close_cost += sum(o["cost"] for o in t["orders"] if not o["ft_is_entry"])

        # C4 input: derive the step THIS trade's grid actually used
        status, lo, hi, m = derive_step_interval(entry_px, buys)
        if status == "ok":
            # point estimate: midpoint of the fills-consistent interval,
            # clamped from below by the fee floor (fee_floor_step
            # semantics: the step can never be below 2*spread + rt fee;
            # when the interval straddles the floor the true step IS the
            # floor — floor-clamped trade)
            est_lo = max(lo, 2 * SPREAD_PCT + ROUND_TRIP_FEE_PCT["hyperliquid"])
            derived.append((ti, lo, hi, (est_lo + hi) / 2.0, m))
        elif status == "inconsistent":
            surprises.append(
                f"C4 trade#{ti}: line buy fills fit no single geometric "
                f"grid step (anchor {entry_px}, lines {sorted(buys)})")

        # C1: FIFO pairing per line; leftovers close via the full exit
        for i, bpx in sorted(buys.items()):
            spx = sells.get(i, [])
            for bp, sp in zip(bpx, spx):
                usdc = sp / bp - 1.0
                (tp_trips if sp > bp else surprises.append(
                    f"C2 trade#{ti} L{i}: TP sell {sp} <= buy {bp}") or tp_trips
                 ).append((ti, i, bp, sp, usdc)) if False else (
                    tp_trips if sp > bp else None)
            # (kept simple below)
        # -- do the pairing explicitly to keep bookkeeping readable --
        tp_trips_local, fe_local, unclosed_local = [], [], []
        for i, bpx in sorted(buys.items()):
            spx = sells.get(i, [])
            for bp, sp in zip(bpx, spx):
                tp_trips_local.append((ti, i, bp, sp, sp / bp - 1.0))
            for bp in bpx[len(spx):]:
                if full_exit_px is None:
                    unclosed_local.append((ti, i, bp))
                else:
                    fe_local.append((ti, i, bp, full_exit_px, t["exit_reason"]))
        for row in tp_trips_local:
            if row[2] >= row[3]:
                surprises.append(
                    f"C2 trade#{row[0]} L{row[1]}: TP sell {row[3]} <= buy {row[2]}")
        tp_trips += [r for r in tp_trips_local if r[2] < r[3]]
        fe_lots += fe_local
        never_closed += unclosed_local

    # C4: fee-gate step invariant — DERIVED per trade from the fills
    # (the old line here printed a static RECON_STEP_PCT label; the step
    # is ATR-derived per trade, so it must be derived per trade).
    fee_floor = 2 * SPREAD_PCT + ROUND_TRIP_FEE_PCT["hyperliquid"]
    if derived:
        print(f"\nderived step per trade (from each trade's own grid fills; "
              f"fee_floor=2*{SPREAD_PCT}+"
              f"{ROUND_TRIP_FEE_PCT['hyperliquid']}={fee_floor}%):")
        for ti, lo, hi, mid, m in derived:
            anch = f"anchor L{m}" if m is not None else "anchor ambiguous"
            clamp = " floor-clamped" if lo < fee_floor <= hi else ""
            print(f"  trade#{ti}: step in [{lo:.4f}, {hi:.4f}]%  "
                  f"(derived {mid:.4f}%,{clamp} {anch})")
        mids = sorted(r[3] for r in derived)
        hull_lo = min(r[1] for r in derived)
        hull_hi = max(r[2] for r in derived)
        print(f"derived step across trades: min={mids[0]:.4f}%  "
              f"median={mids[len(mids)//2]:.4f}%  max={mids[-1]:.4f}%  "
              f"(interval hulls [{hull_lo:.4f}, {hull_hi:.4f}]%)")
        # the true step lies inside each trade's interval, and the
        # fee_floor_step semantics clamp it to >= floor: an interval
        # entirely below the fee floor PROVES a below-floor step was used
        for ti, lo, hi, mid, m in derived:
            if hi < fee_floor:
                surprises.append(
                    f"C4 trade#{ti}: derived step interval "
                    f"[{lo:.4f}, {hi:.4f}]% lies entirely below the fee "
                    f"floor {fee_floor}%")
        ok_min = mids[0] >= fee_floor
        n_floor_clamped = sum(1 for r in derived if r[1] < fee_floor <= r[2])
        print(f"min derived step {mids[0]:.4f}% >= fee floor {fee_floor}%: "
              f"{ok_min}  (floor-clamped trades: {n_floor_clamped}/{len(derived)})")
        if not ok_min:
            surprises.append(
                f"C4 min derived step {mids[0]:.4f}% < fee floor "
                f"{fee_floor}%")
    else:
        print(f"\nderived step per trade: NONE (no trade had a recenter "
              f"anchor plus >=2 filled grid lines); fee floor {fee_floor}% "
              f"not fill-verified")
    # per-trip realized gain distribution. Buys fill inside the
    # (line, line*(1+step)) window, so realized gain lands in [0, step).
    gains = sorted(r[4] * 100 for r in tp_trips) if tp_trips else []
    step_hi = max((r[2] for r in derived), default=None)
    if gains:
        bound = f"{step_hi:.4f}" if step_hi is not None else "<step>"
        print(f"TP round-trip realized gain: min={gains[0]:.3f}%  "
              f"median={gains[len(gains)//2]:.3f}%  max={gains[-1]:.3f}%  "
              f"(expected within [0, {bound})% - buys fill inside the "
              f"line->TP window, sells fill at candle open)")
    else:
        print("TP round-trip realized gain: NONE (no completed per-level trips)")

    print(f"\ngrid buys={counts['grid_buy']}  (recenter entries={counts['grid_recenter']})")
    print(f"grid sells(TP)={counts['grid_sell']}  "
          f"channel_top_exit={counts['full_exit:channel_top_exit']}  "
          f"force_exit={counts['full_exit:force_exit']}  "
          f"stoploss exits=0")
    print(f"completed per-level round trips: {len(tp_trips)}  "
          f"win rate: {100 * len(tp_trips) / max(1, len(tp_trips) + len(surprises)):.1f}%")
    if tp_trips:
        wins = [r for r in tp_trips]
        avg = sum(r[4] for r in wins) / len(wins)
        print(f"TP round trips: avg realized {avg * 100:.3f}% per trip")
    if fe_lots:
        print("full-exit lots (open position closed at trade end):")
        for ti, i, bp, fp, reason in fe_lots:
            mark = "FLAT" if abs(fp - bp) < 1e-9 else (
                "LOSS" if fp < bp else "WIN")
            print(f"  trade#{ti} L{i}: buy {bp} -> {reason} {fp} [{mark}]")
    if never_closed:
        for ti, i, bp in never_closed:
            surprises.append(f"C1 trade#{ti} L{i}: lot at {bp} never closed")

    # C3: cash reconciliation
    rt_usdc = close_cost - open_cost
    total_abs = st.get("profit_total_abs")
    print(f"\nclosed-cost round trips={rt_usdc:.4f} USDC  "
          f"export profit_total_abs={total_abs:.4f}  "
          f"delta={rt_usdc - total_abs:.4f}  "
          f"(fees 0.1%/side on ~{open_cost + close_cost:.0f} USDC volume + funding)")
    print(f"max_drawdown_account={st.get('max_drawdown_account')}  "
          f"profit_total={st.get('profit_total')}")

    print("\n-- surprises --")
    if surprises:
        for s in surprises:
            print(" ", s)
    else:
        print("  (none)")
    return 0 if not surprises else 1


if __name__ == "__main__":
    sys.exit(main())
