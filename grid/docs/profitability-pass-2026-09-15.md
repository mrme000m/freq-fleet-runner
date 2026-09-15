# Profitability pass — 2026-09-15 (resting ladder + trend horizon)

Live audit of the 4-slot dry-run fleet (BTC/ETH/SOL/HYPE ×150 USDC, 1m–5m
band) found the engines healthy but the grid economics leaking; this is the
record of what was found and what changed. Code: `grid/strategies/
GridStrategy.py` (byte-synced to `ft_user_data/strategies/`), tests in
`grid/tests/test_grid_position_adjust.py` + `test_lower_tf_rescale.py`.

## Findings

1. **Window-priced buys harvested ≈0 net.** The M5 "fee-viable window"
   market-bought anywhere in `(line, line×(1+headroom))`. In a falling tape
   every fill lands at the window's TOP edge, so the trip banks
   `step − headroom = cost_floor` — the observed SOL round trips: +0.254%
   and +0.314% gross vs the 0.24% round-trip cost → **+0.0165 USDC net on
   two trips** (≈1bp margin). Fee-positive by design, profitable in no
   practical sense.
2. **freqtrade's `realized_profit` misstates grid harvest mid-trade.**
   Partial exits book against the position's AVERAGE cost, so SOL's two
   fee-positive line trips read as **−0.133** (avg cost 99.45 vs sells at
   98.8). The fleet looked unprofitable while the line engine was (barely)
   positive — and with thin margins the inverse can hide real losses.
3. **The trend gate measured a micro trend.** `EMA26` per slot TF spans
   26min (BTC/1m) to 130min (HYPE/5m); the tuned gate is a ~26h trend
   (EMA26 on 1h). At audit time every slot sat 1.7–2.7% under its 26h EMA
   while the micro gate waved ETH/SOL/BTC entries through — the fleet
   averaged into a falling tape all session (−0.27 net). HYPE idled 7h on
   the micro gate, then the pre-restart engine entered it during a bounce
   that the 26h gate would still have blocked.
4. **freqtrade keeps ONE open order per trade on the adjust path.**
   `handle_similar_open_order` REPLACE-cancels all resting orders when a
   differing one is placed (`freqtradebot.py:1869`, called from
   `execute_entry:987` and `execute_trade_exit:2193`). The first resting
   ladder revision ping-ponged BTC's L4/L5 TP sells every ~36s (log:
   "cancelled to be replaced by new limit order").
5. **`get_valid_price` clamps custom prices to ±2% of the rate**
   (`custom_price_max_distance_ratio`). A TP >2% above the rate would be
   repriced toward market — potentially below the line's cost (HYPE's grid
   ran a 2.15% step, right at the edge). A deep rung >2% below the rate
   would be dragged up off its line.

## Changes (GridStrategy)

- **Resting-ladder pricing.** Grid buys rest as limits AT their line
  (`custom_entry_price` parses the `grid_buy_Li` tag — pos-adjust calls do
  receive it, `freqtradebot.py:1169` only skips `mode=="replace"`); grid
  sells rest at the line TP (`custom_exit_price` parses `grid_sell_Lj`,
  which partial exits receive as `exit_tag`). Every completed trip banks
  the full `step − cost` (the entry gate's `step ≥ min_step_multiple ×
  cost` becomes the per-trip guarantee, not a placement-time hope).
- **Single-order discipline.** One desired order per loop, sticky while it
  rests: sells lowest-line-first once the rate is at/above the line; a
  parked sell swaps to a buy only when the rate falls below the parked
  sell's line (keep laddering down in a falling tape); a resting buy is
  never re-decided. Sell-veto now FALLS THROUGH to the buy branch — the
  pre-fix shape deadlocked (vetoed sell re-decided every loop, buys never
  fired; a 2-lot position could never grow past the remaining-position
  bar).
- **Min-exit-veto flatten (`grid_flatten_Li`).** The 2-lot trap: with
  per-line stakes near the exchange minimum, freqtrade's
  remaining-position veto (`min_stake/(1−|stoploss|)`) blocks EVERY
  one-line reduce from a small position, and the capital-bounded ladder
  depth (`_armed_rungs`, see below) can simultaneously cap the buy branch
  — the position would park until the channel exits. Escape hatch: when
  the per-line sell is vetoed and price reaches the HIGHEST filled line's
  TP, flatten the whole position there (every lot banks ≥ its line → top
  TP, structurally fee-positive). The tag deliberately misses the ledger's
  `grid_(buy|sell)_Li` regex so M4 pairing reads it as the trade's full
  exit (one full_exit trip per lot, no pool-drain surprise).
- **Capital-bounded ladder depth (parallel pass, same commit window).**
  `per_line_size()` floors every rung at the exchange `min_cost`, so the
  ladder-scaled allocation was silently over-committed (alloc $25 → ~$40
  deployed at base tier) and every ladder tier sized identically.
  `_armed_rungs` caps armed rungs at what the budget funds
  (`max_commit_ratio = 1.0`), and `ladder_pct` parsing is now fail-closed
  on corrupt input (previously an unreadable ladder meant the LARGEST
  allocation).
- **Clamp-safe placement envelope** (`_CUSTOM_PRICE_REACH = 1.9%`): orders
  only land on lines inside freqtrade's clamp window; the ladder slides
  with price. Buys tolerate half a step above the rate (marketable gap
  fills still capture the rung at the line price).
- **Trend-horizon rescale** (`_trend_ema_period`): 26 bars × 60/tf_min —
  1m→1560, 3m→520, 5m→312, 1h→exactly 26 (bit-equal to the tuned regime).
  `startup_candle_count` scales to match (1m slot fetches ~1600 bars ≈ 27h).
- **Console per-line harvest.** `/api/overview` instances now carry
  `grid_harvest` / `grid_harvest_24h` / `grid_trips*` — realized cash from
  COMPLETED per-line round trips (FIFO per line, net of config fee),
  computed from the orders table so it works on OPEN trades. The fleet
  card shows it next to freqtrade's average-cost `realized`. Verified
  against the live SOL DB: 0.016522 == hand-computed.

## Expected economics vs observed

| | pre-fix (window) | post-fix (resting) |
|---|---|---|
| trip margin (SOL grid, step 1.07%) | ~0.01–0.07% | 0.83% |
| trip margin (BTC grid, step 0.79%) | ~0 observed | 0.55% |
| fill style | taker at window edge | limit at the line |
| trend gate horizon | 26–130 min | ~26h on every TF |

Trade-off: fewer, fuller trips. The window design filled whenever price
sat inside any window (nearly always); the resting ladder fills on line
touches. Per-dollar-deployed the ladder is ~10–80× more profitable per
trip and structurally cannot churn fees.

## Ops notes

- `grid/dev start`'s 180s readiness probe fires "not ready" on every cold
  start (HL history download takes ~4min; now longer with 352–1600 startup
  bars). It does NOT roll back — engines keep booting. Re-ping before
  believing it.
- The pre-restart engine entered HYPE at 15:35 UTC on the OLD micro gate
  24min before the restart; the position was inherited and is managed by
  the new code (the gate blocks only NEW entries).
- ft-hype's first post-restart engine died 10min after boot:
  `sqlalchemy.exc.TimeoutError: QueuePool limit ...` during API teardown —
  same failure class as the console-poller pool exhaustion (fixed there
  via TTL cache + backoff). Watch for recurrence during boots.
- With the 26h gate, the whole fleet was trend-blocked at restart time
  (all slots 1.7–2.7% under EMA). Flat in a multi-day downtrend is the
  gate working as calibrated, not an outage; fleet-level rotation into
  trending assets is the screen/rotate agents' job (M5 seams), not the
  per-slot gate's.
