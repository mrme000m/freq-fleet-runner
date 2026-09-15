# 2026-09-15 lower-TF reset

> Full restart + timeframe migration: every grid bot now runs in the
> **1m–5m band** instead of 1h, and every screen/swarm LLM call is
> grounded in a **Multi-TF analysis pack (1m + 5m + 15m)** instead of
> the prior single-TF view.

## Why

User directive, 2026-09-15:
- "Deal with 1-5 min charts."
- "All LLMs should base their analysis on lower-TF analysis and data."

The previous system ran on **1h candles only** with the LLM swarm
seeing the same single TF as the strategy — too coarse for the
fast-cycle intent the user wants. This reset pins the engines into
the lower-TF band and gives the LLM multi-resolution context.

## What changed

### Engines — per-slot TF map

| Slot | Pair | TF (new) | TF (prior) | Notes |
|------|------|----------|------------|-------|
| ft-btc  | BTC/USDC:USDC  | **1m** | 1h | most liquid → fastest cycle |
| ft-eth  | ETH/USDC:USDC  | **3m** | 1h | mid-cycle |
| ft-sol  | SOL/USDC:USDC  | **3m** | 1h | mid-cycle |
| ft-hype | HYPE/USDC:USDC | **5m** | 1h | thinner book → slower settle |

Pinned in `grid/dev:SLOT_TIMEFRAMES` and stamped into each instance
config + registry entry under `timeframe`.

### GridStrategy

- `timeframe = "1m"` (default; overridden per-slot by the deployer)
- `startup_candle_count = 60` (was 30) — keeps EMA(26) warm on 1m
- `process_only_new_candles = False` (was True) — don't strand the
  first 1m candle after restart
- **Recenter cooldown is TF-aware** — replaced
  `timedelta(hours=self.recenter_cooldown_candles)` (which made the
  cooldown wrong by 60-300× on the lower-TF band) with a new
  `_tf_timedelta()` helper that maps `"1m"`, `"3m"`, `"5m"` etc. to the
  right wall-clock duration.

### Lower-TF data fetch (grid/screen.py)

- `fetch_candles()` is now TF-aware (`interval="1m"`,
  `lookback_minutes=…` instead of `lookback_hours=…`).
- `screen()`'s default anchor moved from 1h → 5m (mid-cycle volatility
  surface for the same coverage intent).
- **New `multi_tf_pack(coin, tfs=("1m","5m","15m"))`** — fetches a
  bound candle set per TF (with `1m` downsampled every 5th bar) and
  computes a per-TF ATR + a single geometric-mean ATR aggregate. This
  is the single source of candle truth for every swarm call.

### Multi-TF in the swarm (grid/agents/deliber.py)

- **New `attach_multi_tf(brief, coin)`** — fetches the Multi-TF pack
  for `coin` (default = brief symbol), threads a compact summary
  (`{close, atr_pct, trend_pct, n_bars}` per TF) into
  `brief["market_context"]`, and stores the full pack under
  `brief["market_context_full"]` for the rotator.
- Every agent (bull / bear / rebuttal / facilitator / risk) now sees
  the same lower-TF context via the existing `brief_text()` shape —
  no per-agent drift, no extra prompt tokens per agent.
- The helper is **best-effort**: HL outage → swarm falls back to the
  metrics-only path, `llm_degraded: True` (same as the legacy M5 path).

### `grid/dev` (deployer)

- New `SLOT_TIMEFRAMES` map → `engine_config(timeframe=…)` → per-bot
  `config.json["timeframe"]`.
- Registry entry gains `timeframe`; status/start output uses it instead
  of the hardcoded "1h DRY-RUN" label.
- `_swarm_ticket()` calls `attach_multi_tf(brief, coin=coin)` before
  delegating to `deliberate()` so every rotate/evaluate decision is
  grounded in the lower-TF context.

### Mission console (grid/console/server.py + static/app.js)

- `CHART_INTERVALS = ("1m", "3m", "5m", "15m", "1h", "4h", "1d")`
  (was `("15m", "1h", "4h", "1d")`) — surfaces the lower-TF band
  first; 15m stays as the swing anchor the swarm uses as its
  highest-TF context bar.
- Default chart interval on the frontend flipped 1h → 5m; chart-bar
  probe on the fleet row now uses the slot's TF (was always 1h).

## Reset procedure

`./grid/dev reset --yes` (archive-only) + manual `rm -f` of stale
`ft_user_data/data/hyperliquid/futures/*-1h-*` feathers so the engines
start with an empty local cache.

Archive: `grid/state.reset-20260915T031931Z/` (carried over
`engine.json`, `llm.env`).

Re-vendored `GridStrategy.py` + `GridStrategy.json` + `grid_geometry.py`
into `ft_user_data/strategies/` after the strategy edits — the
`test_vendored_sync` guardrail would have caught the drift anyway.

### First-boot candle warm-up

The reset doc originally said to pre-warm via `freqtrade download-data`
— that doesn't work on Hyperliquid. The ccxt Hyperliquid implementation
returns "Historic data not available for Hyperliquid" and the live
engines do not need bulk history anyway: they stream one candle at a
time from the per-tick ccxt `fetch_ohlcv` path and warm up from those
live ticks (`startup_candle_count=60` per slot TF, so 60 min on the
BTC slot, 5×60=300 min on the HYPE slot).

`grid/scripts/prewarm_data.sh` was rewritten to detect this and exit 0
with an explanation rather than fail; the script is still useful on
exchanges that do support historical downloads (Binance, Bybit, OKX,
…). Verified live: all 4 engines return real candles from `/api/v1/
pair_candles` within seconds of start.

## Verified

- 4/4 engines RUNNING: BTC 1m, ETH 3m, SOL 3m, HYPE 5m
  (`grep "Timeframe:" ft_fleet/ft-*/freqtrade.log`).
- All 4 ports `pong`: 8191 / 8192 / 8193 / 8194.
- Console + pocketbase up: 8798 + 8290.
- `pytest grid/tests/ -q` → **97 passed**.
- Smoke: `multi_tf_pack('BTC')` → `[1m, 5m, 15m]` candles;
  `attach_multi_tf(brief)` → `market_context.tfs = {1m, 5m, 15m}`;
  `deliberate(brief)` → `llm_degraded: True, decision: GO` (no LLM
  keys, rule-fallback path).

## Risks / follow-ups

- **Live candles only**: Hyperliquid has no historical OHLCV endpoint
  via ccxt, so the engines don't seed bulk history; they warm up from
  the first 60 live ticks per slot. The first ~60 minutes of a slot's
  ATR(14) + EMA(26) is therefore noisy. If a backtest or hyperopt is
  needed, use a different exchange or fetch the candles via
  `grid/screen.py:fetch_candles()` and feed them in directly.
- **Strategy tuning was 1h-tuned** (band_atr 4.2, step_factor 1.0) —
  **resolved same day** by the dynamic ATR rescaling below: every
  ATR%-denominated param is now interpreted at the 1h reference
  horizon on any slot TF. A lower-TF re-hyperopt stays optional (M6),
  not load-bearing.
- **Ledger is empty** by design (fresh state). The first
  `grid/dev ledger-sync` after a few hours of dry-run will populate
  per-archetype samples at the new TFs.

## Dynamic ATR rescaling (2026-09-15 — same-day follow-up)

The first ~40 min at the lower-TF band produced **zero entries on all
four slots**. Diagnosed live via `/api/v1/pair_candles`: every
ATR%-denominated param (band_atr 4.2, min_atr_pct 0.3, and the
min_step_multiple × cost gate — ≈ **0.36% effective** with taker fees)
is denominated in **1h-horizon** volatility, while raw per-bar ATR% on
the 1m-5m band is ~√(TF/1h) smaller:

| Slot | TF | raw ATR% mean / max (2-3 d) | bars ≥ 0.36% gate |
|------|----|----------------------------|-------------------|
| ft-btc  | 1m | 0.059 / 0.14 | 0 / 1000 |
| ft-eth  | 3m | 0.125 / 0.39 | 2 / 1000 |
| ft-sol  | 3m | 0.130 / 0.28 | 0 / 1000 |
| ft-hype | 5m | 0.262 / 0.63 | 281 / 1000 |

So the fleet could only ever enter on vol spikes — BTC/1m never.

**Fix: every ATR% the strategy consumes is renormalized to the 1h
reference horizon** (`ATR_REF_MINUTES`) by a √-of-time factor
`_atr_scale(timeframe) = sqrt(60 / tf_minutes)` — 1m → 7.746, 3m → 4.472,
5m → 3.464, **1h → exactly 1.0** (the calibrated regime is bit-unchanged;
4h+ scales down symmetrically).

- `populate_indicators` now produces the **reference-horizon ATR%**
  (`atr_pct` column); raw per-bar vol stays recoverable as
  `atr/close×100`.
- Entry gate, channel band, and step size all read that column, so the
  1h-calibrated economics carry to any TF: at the mean vols above the
  slots gate at 0.465 / 0.559 / 0.580 / 0.908 ≥ the 0.36% fee bar, with
  fee-clearing steps (~0.4-0.9%) inside a sane ±1-2% channel instead of
  the ±0.25% pinhole raw 1m vol gives.
- **The M5 profitability invariants are untouched**: the fee-clearance
  bar (min_step_multiple × round-trip taker cost) is TF-agnostic by
  construction — a quiet 1m tape (raw 0.01%/bar → 0.077% at the
  reference horizon) still never enters. Rescaling loosens nothing; it
  only makes the horizon of the measured vol match the horizon the
  gates were calibrated on.
- The console's channel preview (server.py reads the bot's analyzed
  dataframe `atr_pct` and mirrors the same geometry) stays consistent
  automatically.
- `grid/tests/test_lower_tf_rescale.py` pins the contract: scale
  factors, the 0.36% default gate, entry fires at 1m/3m/5m mean vol but
  not at 1h, thin vol stays blocked, fee-clearance + channel-density
  invariants, and 1m/1h grid-identity at the same reference-horizon
  vol (proving it is a horizon transform, not a retune).

Tests: **105 passed** (97 + 8 new). Strategy re-vendored into
`ft_user_data/strategies/` (byte-sync guard) and re-materialized into
the four slots by `grid/scripts/restart.sh` (`engine_start` re-copies
`STRATEGY_FILES` from `grid/strategies/`).
