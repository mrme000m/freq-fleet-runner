"""Cross-validate per-trade step reconstruction against true candle geometry.

Paths anchor at the repo's ft_user_data scratch (override the export with
argv[1]); needs pandas + talib (the .venv-ft python has both).
"""
import json, math, re, sys, zipfile
from pathlib import Path
from collections import defaultdict
import pandas as pd
import talib

LRE = re.compile(r"grid_(buy|sell)_L(\d+)$")

FT_USER_DATA = Path(__file__).resolve().parents[2] / "ft_user_data"
EXPORT = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    FT_USER_DATA / "backtest_results" / "m1_grid.zip"

with zipfile.ZipFile(EXPORT) as z:
    for n in z.namelist():
        if n.endswith(".json") and "_config" not in n and "meta" not in n \
           and "market_change" not in n and "_wallet" not in n:
            d = json.loads(z.read(n))
            if isinstance(d, dict) and "strategy" in d:
                break
key = next(k for k in d["strategy"] if "Grid" in k)
st = d["strategy"][key]

df = pd.read_feather(FT_USER_DATA / "data" / "hyperliquid" / "futures"
                     / "BTC_USDC_USDC-1h-futures.feather")
df["date"] = pd.to_datetime(df["date"], utc=True)
df["atr"] = talib.ATR(df["high"], df["low"], df["close"], timeperiod=14)
df["atr_pct"] = df["atr"] / df["close"] * 100
df = df.set_index("date")

def fee_floor_step(step_pct, spread_pct, venue, step_min=0.1, step_max=2.0):
    rt = {"hyperliquid": 0.10}.get(venue, 0.15)
    return min(step_max, max(step_min, step_pct, 2*spread_pct + rt))

def feasible(entry_px, buys, s_lo=0.02, s_hi=4.0, ds=1e-4):
    idx = sorted(buys)
    if entry_px is None or len(idx) < 2:
        return None
    fills = {i: buys[i][0] for i in idx}
    lo = hi = None; ms = set()
    k = 0
    while s_lo + k*ds <= s_hi:
        s = s_lo + k*ds; k += 1
        lx = math.log(1 + s/100.0)
        m_set = set()
        for i, f in fills.items():
            u = math.log(f/entry_px)/lx
            m_set.add(i - int(math.floor(u + 1e-9)))
            if len(m_set) > 1: break
        if len(m_set) == 1:
            ms |= m_set
            lo = s if lo is None else lo
            hi = s
    return (lo, hi, ms)

step_factor, band_atr = 0.5, 3.0
for ti, t in enumerate(st["trades"]):
    buys = defaultdict(list); entry_px = None
    for o in t["orders"]:
        m = LRE.search(o["ft_order_tag"] or "")
        if o["ft_is_entry"]:
            if m: buys[int(m.group(2))].append(o["safe_price"])
            else: entry_px = o["safe_price"]
    feas = feasible(entry_px, buys)
    lo, hi, ms = feas if feas else (None, None, None)
    od = pd.Timestamp(t["open_date"]).tz_convert("UTC")
    # candidate candles: the open candle and the one before (grid built at entry)
    rows = []
    for off in (0, -1):
        ts = od + pd.Timedelta(hours=off)
        try:
            row = df.loc[ts]
        except KeyError:
            continue
        true_step = fee_floor_step(float(row["atr_pct"]) * step_factor, 0.02, "hyperliquid")
        rows.append((off, float(row["atr_pct"]), true_step))
    ok = feas is not None and any(lo - 1e-9 <= ts_ <= hi + 1e-9 for _, _, ts_ in rows)
    print(f"trade#{ti} open={od} feas={feas} candidates={[(o,round(a,4),round(s,4)) for o,a,s in rows]} match={ok}")
