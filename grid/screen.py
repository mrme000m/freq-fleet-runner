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


def fetch_candles(coin, interval="1h", lookback_hours=30 * 24):
    """Last `lookback_hours` of 1h candles for `coin` (list of dicts)."""
    end = int(time.time() * 1000)
    start = end - lookback_hours * 3600 * 1000
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


def screen(top=30, min_volume=MIN_VOLUME_USD, fetch_candles_n=30):
    """Rank the universe by volatility-weighted volume. Returns the top
    `top` rows as dicts: {coin, pair, pair_code, dayNtlVlm, atr_pct,
    score}."""
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
        cs = fetch_candles(r["coin"])
        r["atr_pct"] = round(atr_pct(cs), 3)
        time.sleep(0.25)
    for r in rows:
        a = r.get("atr_pct") or 0.0
        r["atr_pct"] = a
        r["score"] = round(r["dayNtlVlm"] * a, 2)
        r["pair"] = f"{r['coin']}/USDC:USDC"
        r["pair_code"] = f"{r['coin']}USDC"
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