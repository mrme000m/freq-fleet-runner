# freqtrade Execution Engine — Design & Plan

**Status:** Decision-grade design + implementation plan. This document supersedes
`hummingbot-choice-rationale.md` for the execution-backend choice.

**Directive:** build the autonomous grid-trading system with **freqtrade** as the
execution engine, implemented **locally first** (dry-run / paper), with the
existing agent layer (`screen` → `swarm` → `guardrails` → `deploy` → `watch` →
`optimize` → `rotate` → `reflect` → `reliability`) automating the entire process.

**Source of truth:** verified against `agents/grid-autonomy/` code (`daemon.py`,
`execution/grid_adapter.py`, `execution/guardrails.py`, `execution/observe.py`,
`agents/swarm.py`, `agents/reflect.py`, `config.yaml`, `docs/grid-autonomy-system.md`)
and the freqtrade source + `freqtrade-fleet-manager` plugin in `grid/0/freqtrade`.

---

## 1. The pivot — why freqtrade, local-first

The prior decision chose Hummingbot for its *native* `GridExecutor`. This plan
re-opens that choice on two grounds that have changed since that document:

1. **The dominant need is now local research-first validation, and freqtrade owns
   that.** freqtrade's vectorized backtesting, hyperopt, and FreqAI are the best-in-class
   tooling to validate grid economics (geometry, fee gate, per-level round-trips) on
   historical data *before any capital is deployed*. "Implement locally first" is
   literally freqtrade's home turf: `freqtrade backtesting` + `freqtrade trade --dry-run`
   run with no exchange account and no browser session.
2. **An agent control surface for a freqtrade *fleet* already exists.** The
   `freqtrade-fleet-manager` dsh plugin (56 `ft_*` tools: registry, lifecycle,
   telemetry, mutation, secrets, config, deploy, backtest, hyperopt, download) is a
   working, REST-driven control plane for multiple freqtrade instances — exactly the
   "many grid bots" surface the agent layer needs. The Hummingbot path had no such
   already-built surface; its Condor analog would be net-new.

What does **not** change: the venue-agnostic intelligence in
`grid-autonomy-system.md` §7 (screen, swarm, reflect, guardrails, reliability,
stagnation, position-optimizer, optimizer, llm/provider, spreads) is preserved
byte-for-byte. Only the §6 execution/observation layer is re-homed.

### The honest trade-off (the Hummingbot critique, addressed head-on)

freqtrade has **no native multi-level grid executor**. Its model is one `Trade`
per pair with `adjust_trade_position` (add = DCA entry, reduce = partial exit).
This plan therefore builds a **position-adjustment grid** — a single-position DCA
grid with per-level take-profit via partial exits — not N simultaneous
independent level-pairs. This is a real semantic delta from the WT/Hummingbot
grid and is treated as a first-class risk (§7), with an explicit escalation path
(level-multiplexed fleet) if backtesting shows the delta costs material PnL.

### Licensing note

freqtrade is GPLv3. Running freqtrade as the engine with our **runtime-loaded**
`GridStrategy` (a strategy file in `user_data/strategies/`, loaded by freqtrade at
startup) does not make the strategy a derivative that must be GPL-licensed, and
does not restrict *using* the system — the copyleft obligation attaches only if we
fork and *distribute* a combined work. We run, we do not redistribute; the agent
layer and the strategy remain our own code. If a proprietary distribution is ever
required, the strategy/agent layer is already cleanly separated from the engine.

---

## 2. Target architecture

Unchanged decision loop; new execution substrate.

```
                 ┌──────────────────────────────────────────────┐
                 │               daemon.py (unchanged)          │
                 │        schedule + orchestrate + journal       │
                 └───┬───────────────────────────────────┬──────┘
      screen/swarm/   │ tickets (venue, symbol, channel, │  60s watch
      guardrails/     │ step, sizing, regime)            │
      optimizer/      ▼                                  ▼
      reflect (pure)  ┌──────────────────────────────┐  ┌─────────────────────┐
                      │ execution/freqtrade_adapter  │  │ execution/observe   │
                      │ ticket → freqtrade config +  │  │ ft_status/ft_profit/│
                      │ GridStrategy deploy/rotate   │  │ ft_trades/ft_locks  │
                      └───────────────┬──────────────┘  └──────────▲──────────┘
                                      │ REST (auto-JWT)            │ REST
                                      ▼                             │
                     ┌──────────────────────────────────────────────┴──┐
                     │  freqtrade fleet — one instance per grid bot     │
                     │  (GridStrategy, dry-run first; SQLite trade DB)  │
                     │  control via freqtrade-fleet-manager ft_* tools  │
                     └──────────────────────────────────────────────────┘
```

**Fleet mapping:** one grid bot = one freqtrade instance running `GridStrategy`
on its (venue, symbol) pair. This preserves the existing per-bot lifecycle
(stop → verify → delete → archive → cooldown → deploy) 1:1, and maps directly
onto the `freqtrade-fleet-manager` registry (`ft_instances_*`). A density
optimization (one instance, many pairs, `max_open_trades` = N) is noted in §7 as a
later option; per-bot instances are the primary design because rotation,
per-pair sizing, and per-pair channel geometry stay isolated and testable.

---

## 3. The grid strategy — `GridStrategy(IStrategy)`

The core re-homing: `execution/grid_adapter.compute_upsert` (ATR-band channel,
geometric lines, per-line sizing, fee gate) becomes a freqtrade strategy.

### 3.1 Geometry (re-home `grid_config.build_grid` + `compute_upsert`)

Keep the exact math from `grid_adapter.py`:

- **Channel**: `high = price × (1 + band_atr × atr_pct/100)`, `low = price × (1 − band)`.
- **Geometric lines**: `lines.append(e); e *= (1 + step)` from `low` to `high`
  (append `high` as the last line), with the same `guard < 500` cap.
- **Fee-aware step floor** (corrected at M1 against the source — the floor RAISES
  the step, never lowers it): `step = min(step_max, max(step_min, step × step_mult, 2×spread + ROUND_TRIP_FEE_PCT[venue]))`,
  all in **percent** (`ROUND_TRIP_FEE_PCT` values are already percent — 0.10/0.20 —
  no `/100`; unknown venues default to 0.15). Preserved as strategy state.
- **Sizing**: `per_line = max(alloc_usd / grids_n, min_cost)`; one-sided worst-case
  commitment = `per_line × side_lines` with `side_lines = max(1, (n+1)//2)`,
  computed on the **precision-rounded** per-line amount. Sizing lives in
  `build_ticket_payloads`, not `compute_upsert`.
- **Line-loop condition is percent-vs-percent**: `while e <= high and (high−e)/e×100 >= step_pct` —
  not `e×(1+step) <= high`. Consequence: if the step is wider than the whole
  channel, `low` is never appended and lines = `[high]` only.

This is extracted into a shared, pure helper (`grid_geometry.py`) so the strategy,
the guardrails, and the backtest all consume one implementation — no drift between
what the screen scores, what the guard checks, and what the executor trades.

### 3.2 freqtrade callbacks used

| Callback | Grid role |
|---|---|
| `custom_stake_amount` | per-line USD sizing (min-notional floored), the `amount_per_trade` |
| `custom_entry_price` | limit entry at the closest grid line ≤ mid (initial fill) |
| `adjust_trade_position` | the grid engine: **+stake** when price crosses a lower unfilled line (grid buy), **−stake** when price rises one step above a filled line (grid sell / per-level TP). Requires `position_adjustment_enable = True`; `max_entry_position_adjustment = -1` (unlimited) |
| `custom_exit` / `custom_exit_price` | daemon-owned profit exit (price breaks the top line) and rotation signal |
| `custom_stoploss` | the wide, opt-in risk cap (never-close-at-a-loss is preserved: stoploss is a hard ceiling, not the profit mechanism) |
| `leverage` | per-slot leverage for the HL perp sleeve |

The line state machine (which lines are filled vs. sold) lives in a per-trade
helper keyed by `trade.id`, tagged through the `adjust_trade_position` tuple's
2nd element (`order_reason`, e.g. `grid_buy_L3` / `grid_sell_L2`). This tag is the
bridge to per-level round-trip accounting (§3.4).

Config contract (per instance, generated from the ticket):

```yaml
strategy: GridStrategy
position_adjustment_enable: true
max_entry_position_adjustment: -1
stake_amount: unlimited            # sized per-line in custom_stake_amount
tradable_balance_ratio: 0.85       # matches deployable ceiling (85%)
fee: 0.001                         # venue round-trip fee model (HL 0.10% / BN 0.20%)
unfilledtimeout: { entry: 60, exit: 60, unit: minutes }
```

### 3.3 Entry / exit semantics

- **Initial entry** at the closest line ≤ mid, sized one line.
- **Accumulation**: each downward line-crossing (candle close below the next line)
  triggers `adjust_trade_position` → `+per_line` at that line's limit price.
- **Per-level TP**: each upward crossing of `filled_line × (1 + step)` triggers
  `adjust_trade_position` → `−per_line` (reduce-only), realizing that line's
  round-trip profit.
- **Full exit / rotation**: `custom_exit` fires when price exceeds the channel top,
  regime flips, or the stagnation/rotation policy demands re-center — the daemon
  then performs its existing stop → verify → archive → redeploy round-trip.

### 3.4 Per-level round-trip accounting (re-home `reliability_grid.py`)

freqtrade persists every order and trade in SQLite (`tradesv3.sqlite`), and the
REST surface exposes open trades (`/status`) and history (`/trades`). Each
grid buy/sell carries its `order_reason` tag, so the reliability ledger rebuilds
per-archetype PF/samples by pairing `grid_buy_Li` with its matching `grid_sell_Lj`
fills — the same "closed round-trips" extraction `observe._closed_round_trips`
does against WT history. No WT objects; the ledger feeds the existing sizing
ladder (base 25% / probe 40% / full 50%) and archetype kill-flags unchanged.

---

## 4. Fleet + agent control surface

The agent layer drives the fleet through **`freqtrade-fleet-manager`** (the
56-tool dsh plugin) or a thin direct REST client — same surface, two call paths.

| grid-autonomy module | freqtrade home |
|---|---|
| `daemon.select_profile` / `execution/profiles.py` | `ft_instances_add` (register instance with `exchange`, `dry_run`, `strategy: GridStrategy`) |
| `execution/grid_adapter.grid_create/stop/delete/edit` | `ft_deploy_local` / `ft_start` / `ft_stop` / `ft_instances_remove` (+ `ft_config_generate` to write the per-ticket config) |
| `execution/observe.grid_status/grid_capacity/account_limits/grid_profiles` | `ft_status` / `ft_balance` / `ft_profit` / `ft_health` / `ft_sysinfo` |
| `execution/observe._positions_history` / `_closed_round_trips` | `ft_trades` (+ SQLite `tradesv3.sqlite` for full history) |
| `execution/resolve.py` (`:2087` market map) | **Deleted** — freqtrade's exchange module resolves pairs; min-notional/precision from the exchange |
| `execution/wt_library.py` / `wt_browser.py` / `profiles.py` | **Deleted** — first-party freqtrade connectors |
| `scripts/wt_reset.py` | `ft_backtest_reset` + `ft_instances_remove` sweep (paper only) |
| `guardrails.py` (8 gates) | **Unchanged** — pure predicates; the ticket's `guard_ctx` is now fed from freqtrade config + REST balance instead of WT |
| `screen/merge.py`, `swarm.py`, `reflect.py`, `optimizer.py`, `position_optimizer.py`, `reliability_grid.py`, `policy/stagnation.py`, `spreads.py`, `llm/provider.py` | **Unchanged** (venue-agnostic) |

The `watch` loop (60s) and `watchdog` (browser-session relaunch) are replaced by
freqtrade's own `--dry-run` process + the fleet-manager `/ping`/`ft_health` probes.
No CloakBrowser CDP session, no `:2087` market-map cache, no WT paper-profile
machinery.

---

## 5. Re-homing table (summary)

| grid-autonomy asset | freqtrade home | Notes |
|---|---|---|
| `grid_adapter.compute_upsert` (geometry) | `grid_geometry.py` + `GridStrategy` callbacks | One shared pure geometry module |
| ATR-band + geometric lines | `GridStrategy` line generator | Reuses the exact `compute_upsert` math |
| Per-level TP | `adjust_trade_position` (−stake) | Sequential partial exits, tagged `grid_sell_Li` |
| Fee-aware step gate | `guardrails.check_spread` (unchanged) + `fee` config | Same invariant, one source |
| Min-notional floor / sizing | `custom_stake_amount` + exchange limits | Per-line `max(min_cost, alloc/grids_n)` |
| Sizing ladder (base/probe/full) | per-instance `stake_amount`/`max_open_trades` tier | Ladder logic stays in the daemon |
| `guardrails.py` (8 gates) | unchanged | `guard_ctx` sourced from REST + config |
| `reliability_grid.py` | SQLite trade/order history | `order_reason` tags pair round-trips |
| `observe.py` | `ft_status/ft_profit/ft_trades/...` | Read-only REST, no browser |
| `swarm/reflect/optimizer` | unchanged | Pure reasoning, no venue |
| `screen/merge.py`, `spreads.py` | unchanged | Public OHLCV + book-ticker |
| `resolve.py`, `wt_library.py`, `wt_browser.py`, `profiles.py`, browser watchdog | **deleted** | freqtrade connectors + REST replace them |

---

## 6. Local-first implementation plan

Milestones are ordered so the execution engine is proven in freqtrade **before**
the agent layer is wired to it.

### M0 — Environment (local freqtrade)
- Stand up a local freqtrade (the `grid/0/freqtrade` checkout, stable tag) with a
  `user_data` dir; confirm `freqtrade backtesting` and `freqtrade trade --dry-run`
  run offline.
- Confirm the `freqtrade-fleet-manager` plugin registers and `ft_ping`/`ft_status`
  work against a local `--dry-run` instance.

### M1 — Spike `GridStrategy` + backtest (the one port)
- Extract `grid_geometry.py` from `grid_adapter.compute_upsert` (pure, unit-tested
  against the existing `tests/` fixtures for channel/lines/sizing).
  **Done (2026-09-14):** `execution/grid_geometry.py` + `tests/test_grid_geometry.py`
  (42 tests), reproducing the live-bot fixture `tests/fixtures/grid_resource.json`
  exactly (13 levels, closest levels 86.862418225744 / 87.374906493276). M0 done the
  same day: `.venv-ft` (python 3.13, editable install of the `develop` checkout, no
  dep fixes needed) + 90 days of 1h BTC/USDC:USDC hyperliquid futures candles in
  `ft_user_data/` (gitignored scratch at the freq-fleet-runner repo root).
- Implement `GridStrategy` with the §3 callbacks. Order tags propagate: the
  `adjust_trade_position` tuple's 2nd element lands in `Order.ft_order_tag` for both
  position-adjust entries and partial exits (verified in
  `freqtradebot.check_and_call_adjust_trade_position`).
- **Acceptance:** `freqtrade backtesting` on one Hyperliquid perp pair reproduces
  the screen's EV-harvest expectation; per-level round-trips reconcile with the
  fee-gate invariant (step clears `2×spread + fees`).
- **M1 RESULT (2026-09-14, verified):** `GridStrategy` spike at
  `ft_user_data/strategies/GridStrategy.py` (freq-fleet-runner scratch, gitignored)
  backtested on BTC/USDC:USDC 1h hyperliquid futures, 20260701–20260913, 1000 USDC
  dry wallet: 11 trades, 10W/1L, +2.72 USDC (+0.27%), max DD 0.25%; exits 10
  `channel_top_exit` + 1 range-end `force_exit`, 0 stoploss. Reconcile
  (`ft_user_data/m1_reconcile.py`, exit 0, zero surprises): 27 `grid_buy_Li` +
  23 `grid_sell_Lj` TP fills, 23/23 completed per-level round trips at 100% win
  (realized gain min 0.014% / median 0.329% / max 2.584%); fee-gate invariant
  confirmed (step 0.6% ≥ floor 0.14% = 2×0.02 spread + 0.10 HL fee) from both
  geometry and fill prices; cash delta vs export total = fees + funding (1.60 USDC
  on ~1104 USDC volume). Behavioral surprises logged for M3/M4:
  (1) `custom_entry_price` also fires for every position-adjust order — must
  early-return `proposed_rate` when `trade` is not None;
  (2) `--export-filename` is ignored (timestamped zips only);
  (3) backtest adjust orders fill at candle OPEN;
  (4) the literal "highest line below rate" buy rule re-buys stale low lines above
  their TP — buys must be confined to the `(line, line×(1+step))` window;
  (5) range-end `force_exit` closes open lots at a loss (artifact, not strategy);
  (6) one-shot-per-line-per-trade: `pending_sell` lines are not re-sellable in
  the spike (M4 ledger must model this or the strategy must reset per-line state).

### M2 — Hyperopt the grid
- `freqtrade hyperopt` over `band_atr`, `step_factor`, `step_min/max`, sizing tier.
- **Acceptance:** hyperopt results feed `config.yaml:grid_defaults`; the existing
  `optimizer.py`/`position_optimizer.py` remain the runtime arbiters.
- **M2 RESULT (2026-09-14, verified):** `GridStrategy` geometry is hyperoptable
  (`band_atr` 1.5–5.0, `step_factor` 0.2–1.0, space `buy`; step = fee-floor-clamped
  `atr_pct × step_factor`, mirroring the daemon derivation — so results map 1:1 onto
  `grid_defaults`). 150 epochs, SharpeHyperOptLoss, BTC/USDC:USDC 1h HL
  20260701–20260913, 2:37 runtime. **Best epoch 120: band_atr=4.2, step_factor=0.21
  (Sharpe 9.0)** — backtest 10 trades 10W/0L, **+9.86 USDC (+0.99%)**, max DD 0.0,
  exits 9 channel_top_exit + 1 range-end force_exit. Reconcile: **127/127 per-level
  round trips won**, avg 0.457%/trip, derived per-trade step min 0.1401% ≥ 0.14% fee
  floor (5/10 trades floor-clamped — the fee gate binds as designed).
  Comparison: M1 fixed-step +2.72 USDC (23 trips) → defaults (3.0/0.5) +8.23 (71
  trips) → tuned (4.2/0.21) +9.86 (127 trips). `config.yaml grid_defaults` updated
  to band_atr 4.2 / step_factor 0.21 (uncommitted; step_min/max unchanged — no
  evidence to move them). Learnings: (1) with small steps, per-line stake can fall
  below the exchange `min_stake` and adjust orders are **silently skipped** — the
  strategy now lifts adjust stakes to min_stake; (2) median 1h atr_pct on this
  window is 0.4834%, so M1's fixed 0.6% step ≈ step_factor 1.24 — outside the
  daemon's [0.2, 1.0] range; ATR-derived stepping is materially better; (3) the
  reconcile derives per-trade step by solving for the (step, anchor) consistent
  with all line-buy fills — robust against fill-position noise; freqtrade trims
  data to `timerange − startup_candle_count`, shifting Wilder-ATR near the start.

### M3 — Dry-run loop wired to the agent layer
- `execution/freqtrade_adapter.py` (ticket → `ft_config_generate` + `ft_deploy_local`
  + `ft_start`); `execution/observe.py` re-pointed at `ft_*` REST.
- Daemon runs the full loop (`screen → swarm → guard → deploy → watch → optimize →
  rotate → reflect`) against a **dry-run** local fleet of 2–4 instances.
- **Acceptance:** end-to-end dry-run cycles with `state/decisions.jsonl` +
  run cards; zero WT code paths executed.

### M4 — Reliability re-home
- Rebuild `reliability_grid.py` on freqtrade SQLite order/trade history using
  `order_reason` tags; feed the sizing ladder + kill-flags.
- **Acceptance:** ledger matches M1's backtest round-trip expectations.

### M5 — Paper fleet (multi-instance, still local)
- Fleet of per-bot instances managed via `freqtrade-fleet-manager`; validate
  rotation (stop → archive → redeploy) and `ft_fleet_overview` health sweep.
- **Acceptance:** multi-instance paper run over ≥ a week of live-ish data with
  clean cooldown/hysteresis behavior.

### M6 — (later) remote/live + containerization
- `host=ssh` instances via `ft_deploy_ssh`; the `dsh-ffm` container hosts the
  agent + fleet-manager so the self-modification round-trip (§Agents.md) applies.
- Lift `dry_run` **only** on explicit human confirmation, behind the unchanged
  reliability gate (≥30 samples, PF ≥1.3, recent PF ≥1.0).

---

## 7. Risks, trade-offs, open decisions

| Risk | Mitigation |
|---|---|
| **Single-position DCA grid ≠ N simultaneous levels.** Fills are sequential (candle-close reaction + one resting limit at a time), not market-maker resting orders. | Accept for M1–M5; measure against backtest vs. the WT grid's realized round-trips. Escalation path if the delta is material: **level-multiplexed fleet** — split the channel into level bands, run one freqtrade instance per band (a per-level scalp strategy), recovering simultaneous levels at the cost of N× instances. |
| **Candle-driven entry latency** (signal on candle close, not tick). | For 1h band / 0.1–2% steps this is acceptable; `adjust_entry_price` re-prices a stale resting limit; `unfilledtimeout` cancels. |
| **`adjust_trade_position` partial-exit accounting** must match the ledger. | Tag every order via the tuple's `order_reason`; M4 reconciles against backtest. |
| **GPLv3** (see §1). | Run, don't redistribute; strategy/agent layer stay runtime-loaded and separated. |
| **Dry-run ≠ paper-fill realism** (freqtrade dry-run simulates fills at prices). | M5 paper + backtest cross-check; the fee/spread gate remains the hard floor. |

**Open decisions to confirm before M1:**
1. One instance per bot (primary, recommended) vs. one instance / many pairs (`max_open_trades = N`) — affects fleet density and rotation ergonomics.
2. Exact `custom_exit` profit-target ownership: keep the daemon as the sole exit authority (never-close-at-a-loss) vs. allow `custom_exit` to trigger the top-line profit exit directly.
3. Target freqtrade tag (stable `2025.x` vs. the `develop` checkout) for the local M0 environment.

**Next concrete step:** M1 — extract `grid_geometry.py` and spike `GridStrategy`,
then run `freqtrade backtesting` on one Hyperliquid perp pair to confirm the
geometry + fee-gate invariant before any broader migration.