# Reliability Ledger (M4) — freqtrade order-tag round-trip pairing

**Status:** implemented + accepted (2026-09-15). `grid/reliability/`
(`pairing.py` + `ledger.py`), tests in `grid/tests/test_reliability_ledger.py`
+ `test_m4_acceptance.py`.

**Milestone clause** (roadmap §M4): *"Rebuild reliability_grid.py on
freqtrade SQLite order/trade history using order_reason tags; feed the
sizing ladder + kill-flags. Acceptance: ledger matches M1's backtest
round-trip expectations."*

## What it replaces and preserves

The WunderTrading ledger (`grid/reliability/wt_reference.py` — frozen copy
of grid-autonomy `execution/reliability_grid.py`) counted closed
positions-history rows fetched through the WT browser session. The rebuild
pairs round-trips natively from freqtrade's own order tags — no WT
objects, no browser — and keeps every ledger semantic the daemon gates on:

| WT reference | M4 rebuild |
|---|---|
| positions-history rows (`profitLoss`, `exitedAt`) | trips paired from `Order.ft_order_tag` (`grid_buy_Li` / `grid_sell_Lj`, tag-less full exits) |
| `archetype_stats` / `_stats` shape | identical fields + grid-native `tp_trips`, `full_exit_trips`, `avg_trip_gain_pct`, `updated_at` |
| synthetic (backfill-N seed) rows never gate the ladder | backtest-zip sources default `synthetic=True`; only dry-run SQLite is real evidence |
| `RECENT_WINDOW=20`, PF cap 99, zero-sample no-erase, archive bound 500/archetype, `(strategy_id, close_ts)` dedup | all preserved |
| `ledger_key()` canonical archetype labels | identical table (imported from wt_reference — single source) |

Daemon compatibility: `size_multiplier()` (base 25% → probe ≥10 →
full ≥30 & PF ≥1.3) and `refuse_new_archetype()` (kill at ≥10 samples
& recent_pf < 1.0) are mirrored standalone in `ledger.py` so the
contract is testable here; the daemon can import or keep its own.

## Pairing model

One freqtrade trade = one fungible position; every entry order is a lot
`{line, buy_px, qty}`. Per closed trade:

- `grid_sell_L<i>` drains the line's own lots FIFO, then — for the
  amount-rounding overshoot (freqtrade converts the per-line stake to a
  base amount at the fill price, rounded to pair precision; sells can
  exceed their line's buy) — the trade-wide pool, oldest lot first.
  An undersold line's leftover sliver merges into the same trip at the
  trade's exit price (one lot, one journey).
- Tag-less exit orders (`channel_top_exit` / `force_exit` / stoploss)
  close every remaining lot: **one full-exit trip per lot**, at the exit
  fill, inheriting `exit_reason`. The anchor (`grid_recenter`, fills
  exactly on a grid line, never sold by a grid sell in the spike's state
  machine) closes here too.
- `is_open` trades are skipped (unrealized PnL is not evidence).
- Surprises (never-closed lots, sells with no line buy, sells exceeding
  the whole position) are reported, never silently dropped.

Per-trip `pnl_usd` nets per-leg fees at the trade's `fee_open` /
`fee_close` rates and reconciles to freqtrade's own accounting exactly
(see acceptance). `gain_pct` stays the fee-less line price ratio — the
number the m1_reconcile oracle reports.

The M1 learning *"one-shot-per-line-per-trade: pending_sell lines are
not re-sellable (M4 ledger must model this)"* is handled naturally: the
FIFO queues pair whatever the strategy actually filled; a future
strategy that re-fills a line pairs through unchanged.

## Acceptance (vs the m1_reconcile.py oracle, exit 0 / zero surprises)

Tuned M2 export (`ft_user_data/backtest_results/m1_grid.zip` — 10 closed
trades, +9.86 USDC):

- **127 tp trips, every one a positive price gain, avg 0.457%/trip** —
  matches the oracle exactly; **21 full-exit trips** (11 unsold line
  lots + 10 anchors); zero surprises.
- **Cash reconciliation:** Σ(trip pnl) + Σ(trade funding_fees) =
  9.8673 vs export `profit_total_abs` 9.8601 — 0.0072 USDC (0.07%,
  amount-precision rounding on precision-rounded base amounts).

Default-params export (`m2_default_params.zip`, the M2 comparison row
"defaults +8.23, 71 trips"): **71 tp trips**, PnL reconciles to
<0.01 — after the fungible-overshoot drain (the naive min()-clamp
model was off by 0.069 on this export).

Synthetic semantics: the same export ingested without `!!real` yields
`samples=0, synthetic_samples=148` — research evidence can never move
the ladder or the kill gate.

## Finding surfaced by the ledger (feeds M5)

With the backtest config's fee model (`fee: 0.001` = 0.1%/side → 0.2%
round trip), **41 of the tuned run's 127 tp trips are net-negative
after fees** despite every line clearing the price-gain invariant: the
geometry fee floor uses maker rates (`ROUND_TRIP_FEE_PCT hyperliquid
0.10%` total ≈ 0.05%/side) while the backtest charges 0.1%/side, so
floor-clamped trades (5/10 in the tuned run) harvest steps as thin as
0.14% — below the modeled cost. Either the fee floor must assume
taker/tier pricing on the venue actually traded, or the config's fee
should model maker fills. This is a strategy/economics decision (M5
scope), not a ledger bug — the ledger exists precisely to surface it.

## Use

```sh
python3 -m grid.reliability.ledger --help
python3 -m grid.reliability.ledger \
    --sqlite <instance>/tradesv3.sqlite="Long Grid / classic LONG" \
    --save-archive grid/state/reliability_archive.json \
    --out grid/state/reliability.json --report
```

Sources repeat and take `PATH=ARCHETYPE` labels (regime names
canonicalize through `ledger_key()`); `!!real` / `!!synthetic` override
the default evidence class. The M3 daemon's 24h reliability cron calls
the same `pairing.from_sqlite()` → `ledger.update()` seam once wired.
