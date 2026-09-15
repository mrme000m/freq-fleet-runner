# Grid position-adjustment desync fix — 2026-09-15

Live dry-run evidence (4 slots, ~8h session before the fix, archived at
`state.reset-20260915T141600Z/`): every slot's grid state desynced from
its real inventory, realized PnL was fee-negative on every completed
round-trip, and ~45% of deployed capital ended in "orphaned" lots whose
line TP could never fire again.

## Symptoms observed

1. **8 vetoed sells, 2 per slot** — freqtrade logged
   `Remaining amount of ~10 would be smaller than the minimum of 14`
   each time the grid decided a sell while the position was small.
   `adjust_trade_position` had already mutated the line state at
   decision time (`filled.discard`, `refills += 1`), so every veto left
   a phantom "sold" mark: refill budget burned with no revenue, then a
   double re-buy of the line (2 lots, 1 line mark).
2. **Sell of never-bought inventory** — a grid buy that rested unfilled
   and was canceled still counted as "held" (decision-time
   `filled.add`); the next candle the strategy sold a *different* line's
   inventory at that line's TP, at a loss (BTC 13:33-13:34 UTC; SOL
   08:12 UTC).
3. **Timed-out TP orphan** — a real TP order rested 60m
   (`unfilledtimeout: exit 60`) and was canceled "due to timeout" with
   the line already marked sold → orphaned lot.
4. **Fee-negative churn** — buy windows reached to the line TP, so buys
   filled at window edges harvested ~0.01-0.2% gross against the 0.24%
   taker round-trip cost. All 9 realized round-trips of the session were
   net-negative; the ledger's first closed-trade samples (7) measured
   **E = −$0.19/trip, PF 0.0**.

## Root causes

- `GridStrategy.adjust_trade_position` mutated `filled` / `refills`
  **before** freqtrade confirmed the order (fill, veto, timeout, or
  replacement all happen after the callback returns).
- freqtrade calls `adjust_trade_position` every loop even while orders
  rest open, so a decided-but-unfilled order could be re-decided.
- Buy windows were not fee-viable at the edge: fresh lines used
  `(line, line×(1+step))`, refills `step/2` — both looser than the
  cost bar when step is only ~1.5-2× the taker round-trip cost.

## Fix (`grid/strategies/GridStrategy.py`)

- **Confirm-then-mutate**: `filled` / `refills` change ONLY in
  `order_filled` (freqtrade's fill callback — live, dry-run and
  backtest all route through it), idempotently per `order_id`.
  `adjust_trade_position` is now decision-only.
- **Pending-order tracking**: each adjust call derives lines with
  resting (open) grid orders from `trade.orders` and skips them (no
  double sell, no double buy); pending buys also count toward the
  `max_buys` budget and the `max_stake` headroom.
- **Min-exit pre-check**: `_sell_viable` mirrors freqtrade's
  remaining-position veto (`min_exit_stake = min_stake / (1−|stoploss|)`,
  +2% safety) so a decided sell is one the bot will actually place. A
  skipped sell retries on a later candle with the line still correctly
  marked held.
- **Fee-viable buy windows**: every buy (fresh or refill) is capped at
  `line × (1 + (step − cost_floor))`, so the worst-edge fill still
  clears the round-trip cost; the entry gate already guarantees
  `step ≥ min_step_multiple × cost`, keeping the headroom positive.

Regression tests: `grid/tests/test_grid_position_adjust.py` (suite: 114
passing). The runtime copy `ft_user_data/strategies/GridStrategy.py` is
re-synced byte-identical (vendored-sync contract).

## Companion fixes

- **Console realized PnL** (`grid/console/server.py`): per-instance
  `realized` and the PnL journal's trailing live point now include the
  partial-exit realized held inside open trades — freqtrade keeps the
  cumulative there in `realized_profit` (`close_profit_abs` on an open
  trade is only the LAST exit's chunk); closed trades contribute their
  final `close_profit_abs`. Engine REST `profit_abs` is mark-only, so
  realized + mark now sum to the true net with no double count.
- **Ledger cadence**: the `com.tvcli.grid-ledger-sync` LaunchAgent (6h
  StartInterval) spawns straight into exit 78 / EX_CONFIG on this
  external-volume setup (no stdio file produced, `kickstart -p` aside,
  while the identical command runs clean from the user's context), which
  left `state/reliability.json` stale all day. The console now owns the
  cadence: a daemon thread runs `grid/dev ledger-sync` every 30 minutes
  in-process (verified firing, exit 0, writing
  `state/logs/ledger-sync.log`). The LaunchAgent stays installed as a
  harmless belt-and-braces.

## Post-fix verification (fleet reset 14:16 UTC)

Fresh slate (archived the corrupted session); engines run the fixed
strategy (hash-verified across all four instance copies). BTC entered
via `grid_buy_L4` at 76341 inside the fee-viable window (headroom 0.55%
= step 0.79% − cost 0.24%); `order_filled` confirmed the line mark and
the inventory matches the state exactly. Zero
`Remaining amount … smaller than the minimum` lines anywhere.
