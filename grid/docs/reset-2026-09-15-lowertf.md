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
download fresh 1m / 3m / 5m bars on first boot.

Archive: `grid/state.reset-20260915T031931Z/` (carried over
`engine.json`, `llm.env`).

Re-vendored `GridStrategy.py` + `GridStrategy.json` + `grid_geometry.py`
into `ft_user_data/strategies/` after the strategy edits — the
`test_vendored_sync` guardrail would have caught the drift anyway.

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

- **First-boot data download is slow**: 90d × 1m for BTC alone is
  ~130k bars, and 4 instances download in parallel off the shared HL
  rate budget. Engines may take a few minutes to fully warm on a
  fresh machine; the screen shows `starting` until the first candle
  arrives. If the system is restarted frequently, pre-warm via
  `freqtrade download-data --pairs BTC/USDC:USDC ETH/USDC:USDC SOL/USDC:USDC HYPE/USDC:USDC --timeframe 1m 3m 5m --days 90`.
- **Strategy tuning was 1h-tuned** (band_atr 4.2, step_factor 1.0).
  The M5 pass invariants (taker-fee floor, entry filter, trend gate)
  are TF-agnostic, but the *band_atr* defaults were chosen for 1h
  volatility; re-hyperopt for the 1-5m band is the obvious next
  milestone (M6).
- **Ledger is empty** by design (fresh state). The first
  `grid/dev ledger-sync` after a few hours of dry-run will populate
  per-archetype samples at the new TFs.
