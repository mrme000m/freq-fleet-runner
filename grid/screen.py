#!/usr/bin/env python3
"""grid/screen.py — minimal universe screen for the standalone fleet.

Ranks Hyperliquid USDC-perp assets by volatility-weighted volume
(dayNtlVlm × ATR%) so `grid/dev rotate` can swap weak slots onto liquid,
volatile pairs. Stdlib-only (urllib); hits the public Hyperliquid info
REST API — no ccxt, no keys, no auth, no orders.

A grid wants BOTH: enough 24h notional volume to fill (liquidity) AND
enough ATR% for the geometric step to clear the taker fee floor
(volatility). The product score surfaces the intersection; a low-score
pair is either dead or too flat to harvest.

Usage:
  python3 grid/screen.py [--top 30] [--fetch 30] [--out path]

Output: a ranked table + a screen cache JSON (consumed by
`grid/dev rotate`). Purely observational — no state changes beyond the
cache file.
"""
import argparse
import json
import time
import urllib.request
from pathlib import Path

INFO_URL = "https://api.hyperliquid.xyz/info"
DEFAULT_STATE = Path(__file__).resolve().parent / "state"
MIN_VOLUME_USD = 1_000_000.0   # skip dead pairs (24h notional floor)


def _post(payload, timeout=15):
    req = urllib.request.Request(
        INFO_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_meta():
    """(universe, asset_ctxs) — index-aligned lists from metaAndAssetCtxs."""
    meta, ctxs = _post({"type": "metaAndAssetCtxs"})
    return meta.get("universe", []), ctxs


# Lower-TF analysis pack: the screen/swarm prompt gets 1m + 5m + 15m
# candles per coin so every LLM decision sees structure (15m trend),
# mid-cycle (5m regime), and execution detail (1m microstructure).
# The prior 1h setup gave the swarm only one granularity — too coarse
# for the lower-TF band the engines now trade on.
ANALYSIS_TFS = ("1m", "5m", "15m")
# How much wall-clock history per TF: keeps the prompt bounded but rich
# enough that the LLM sees at least one regime cycle on every TF.
ANALYSIS_TF_WINDOWS = {"1m": 60, "5m": 48, "15m": 96}  # 1h / 4h / 24h
# Step in candles between consecutive datapoints fed to the LLM
# (full-resolution 1m × 60 would be 60 points × 3 TFs = 180 numbers;
# we downsample 1m to every 5th candle so the pack stays small).
ANALYSIS_TF_STEP = {"1m": 5, "5m": 1, "15m": 1}

# Hyperliquid candleSnapshot interval strings. Single source of truth
# so the screen/swarm can't drift from the freqtrade slot TFs.
HL_INTERVALS = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m",
                "1h": "1h", "4h": "4h", "1d": "1d"}

# Per-candle duration in minutes (used to size the lookback window in
# milliseconds without hand-tuning each TF).
_TF_MIN = {"1m": 1, "3m": 3, "5m": 5, "15m": 15,
           "1h": 60, "4h": 240, "1d": 1440}


def fetch_candles(coin, interval="1m", lookback_minutes=None,
                  lookback_hours=None):
    """Last `lookback_minutes` (or `lookback_hours` for back-compat) of
    candles for `coin` at `interval`. Defaults to 60 minutes of 1m bars
    (the new lower-TF default post-2026-09-15 reset)."""
    if lookback_minutes is None:
        lookback_minutes = (lookback_hours * 60) if lookback_hours else 60
    end = int(time.time() * 1000)
    start = end - lookback_minutes * 60 * 1000
    req = {"type": "candleSnapshot",
           "req": {"coin": coin, "interval": interval,
                   "startTime": start, "endTime": end}}
    try:
        return _post(req)
    except Exception:
        return []


def atr_pct(candles, n=14):
    """ATR (n=14) as a percent of the last close, from HL candle dicts."""
    if len(candles) < n + 1:
        return 0.0
    closes = [float(c["c"]) for c in candles]
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i]["h"])
        l = float(candles[i]["l"])
        pc = closes[i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[-n:]) / n
    return atr / closes[-1] * 100.0 if closes[-1] else 0.0


def _resample(candles, step):
    """Take every Nth candle (1=keep all). Pure list slice — the LLM
    only needs the trend, not every print."""
    if step <= 1:
        return candles
    return candles[::step]


def multi_tf_pack(coin, tfs=None):
    """Fetch (1m + 5m + 15m) candles for `coin` and shape them into a
    compact dict for LLM analysis: `{coin, tfs: {tf: {close, atr_pct,
    points: [(ts, o, h, l, c)]}}, atr_pct: <aggregate>}`.

    The pack is the only candle surface the swarm sees — one fetch per
    coin per analysis pass, no double-counting across agents."""
    tfs = tfs or ANALYSIS_TFS
    out = {"coin": coin, "tfs": {}}
    for tf in tfs:
        minutes = ANALYSIS_TF_WINDOWS.get(tf, 60)
        step = ANALYSIS_TF_STEP.get(tf, 1)
        cs = fetch_candles(coin, interval=tf, lookback_minutes=minutes)
        cs = _resample(cs, step)
        pts = []
        for c in cs:
            try:
                pts.append((int(c["t"]),
                            float(c["o"]), float(c["h"]),
                            float(c["l"]), float(c["c"])))
            except Exception:
                continue
        out["tfs"][tf] = {
            "close": pts[-1][-1] if pts else 0.0,
            "atr_pct": round(atr_pct(cs), 3),
            "points": pts,
        }
        time.sleep(0.15)  # gentle on the HL rate budget; pack = 3 calls
    # Aggregate ATR across TFs (geometric mean so thin 15m and thick
    # 1m contribute equally to the screen's volatility signal).
    atrs = [v["atr_pct"] for v in out["tfs"].values() if v["atr_pct"] > 0]
    out["atr_pct"] = round((sum(atrs) / len(atrs)) if atrs else 0.0, 3)
    return out


def screen(top=30, min_volume=MIN_VOLUME_USD, fetch_candles_n=30,
           interval="5m"):
    """Rank the universe by volatility-weighted volume. Returns the top
    `top` rows as dicts: {coin, pair, pair_code, dayNtlVlm, atr_pct,
    score, interval}.

    `interval` defaults to 5m (mid-cycle anchor post-2026-09-15 reset)
    instead of the prior 1h — same coverage intent (a day's worth of
    bars) but at the lower-TF resolution the engines now run on. The
    LLM swarm gets the full Multi-TF pack via multi_tf_pack() on the
    candidates it actually evaluates."""
    universe, ctxs = fetch_meta()
    rows = []
    for i, coin in enumerate(universe):
        name = coin.get("name")
        ctx = ctxs[i] if i < len(ctxs) else {}
        vol = float(ctx.get("dayNtlVlm") or 0.0)
        if name and vol >= min_volume:
            rows.append({"coin": name, "dayNtlVlm": vol})
    rows.sort(key=lambda r: r["dayNtlVlm"], reverse=True)
    # candles only for the top-volume names (bounded HTTP, gentle on HL)
    for r in rows[:fetch_candles_n]:
        cs = fetch_candles(r["coin"], interval=interval,
                           lookback_minutes=24 * 60)
        r["atr_pct"] = round(atr_pct(cs), 3)
        time.sleep(0.25)
    for r in rows:
        a = r.get("atr_pct") or 0.0
        r["atr_pct"] = a
        r["score"] = round(r["dayNtlVlm"] * a, 2)
        r["pair"] = f"{r['coin']}/USDC:USDC"
        r["pair_code"] = f"{r['coin']}USDC"
        r["interval"] = interval
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[:top]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top", type=int, default=30,
                    help="how many ranked pairs to report")
    ap.add_argument("--fetch", type=int, default=30,
                    help="how many top-volume coins to fetch candles for")
    ap.add_argument("--out", default=str(DEFAULT_STATE / "screen_cache.json"))
    args = ap.parse_args(argv)
    rows = screen(top=args.top, fetch_candles_n=args.fetch)
    print(f"{'coin':<8} {'pair':<20} {'24hVol$':>12} {'ATR%':>7} "
          f"{'score':>14}")
    for r in rows:
        print(f"{r['coin']:<8} {r['pair']:<20} {r['dayNtlVlm']:>12,.0f} "
              f"{r['atr_pct']:>7} {r['score']:>14,.0f}")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "min_volume": MIN_VOLUME_USD,
        "pairs": rows,
    }, indent=2) + "\n")
    print(f"\nscreen cache: {out}")


if __name__ == "__main__":
    main()