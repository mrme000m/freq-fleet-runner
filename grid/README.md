# grid/ — the grid-trading core (standalone)

The freqtrade-native heart of the autonomous grid trading system. This
tree is self-contained: no imports reach outside the repo, and the
vendored files below are kept byte-identical by
`tests/test_vendored_sync.py`.

```
execution/grid_geometry.py   pure geometry math: ATR-band channel, geometric
                             grid lines (percent-vs-percent loop, guard<500),
                             fee-floor step (RAISES the step to
                             2×spread + round-trip fee), per-line sizing,
                             one-sided worst-case commitment. Extracted
                             from the daemon's grid_adapter; unit-grounded
                             against a real deployed grid-bot fixture
                             (tests/fixtures/grid_resource.json).
strategies/GridStrategy.py   single-position DCA grid (M1/M2 verified):
                             ATR channel + geometric lines, per-line TP
                             via adjust_trade_position (tags grid_buy_Li /
                             grid_sell_Lj), channel-top full exit. ATR% is
                             renormalized to the 1h reference horizon
                             (√-of-time per slot TF — see
                             docs/reset-2026-09-15-lowertf.md) so the
                             1h-tuned gates/geometry carry to the 1-5m band.
strategies/GridStrategy.json M2-tuned hyperopt params (band_atr 4.2,
                             step_factor 1.0 taker re-base from M5; the M2
                             Sharpe 9.0 run used the maker-modeled 0.21 —
                             see docs/reliability-ledger.md).
strategies/grid_geometry.py  vendored copy — GridStrategy imports the
                             geometry from its own directory first.
reliability/                 M4 — see docs/reliability-ledger.md.
console/                     the mission console (web interface), copied
                             verbatim from the WT-based system (see
                             "Console" below).
docs/                        freqtrade-execution-engine.md = the M0–M6
                             roadmap (copied; M0–M2 results recorded in
                             it), reliability-ledger.md = M4 record.
tools/                       m1_reconcile.py (backtest-export invariant
                             oracle: C1–C4, exit 0 = clean),
                             crossval_step.py (per-trade step derivation
                             cross-check). Paths are CLI-parameterized;
                             the scratch originals in ft_user_data/ stay.
tests/                       76 tests: 42 geometry, M4 ledger/gates/
                             archive/CLI, vendored sync, and M4
                             acceptance (skipped where the ft_user_data
                             scratch is absent).
```

## Geometry resolution chain (why the strategy has two imports)

GridStrategy imports the SAME geometry module in all locations; the chain
is vendored-first, legacy-fallback:

1. `grid_geometry.py` vendored next to the strategy file. freqtrade loads
   strategies inside `with PathModifier(module_path.parent)`, so a plain
   `import grid_geometry` resolves while the module body executes.
2. The legacy grid-autonomy checkout (absolute path). The M3 daemon's
   `freqtrade_backend.py` copies ONLY `GridStrategy.py` +
   `GridStrategy.json` (its `STRATEGY_FILES`) into per-instance strategy
   dirs, so instance copies have no vendored sibling and must resolve
   the module from the original source location.

Because of that constraint, **all copies of `grid_geometry.py`,
`GridStrategy.py`, and `GridStrategy.json` must stay byte-identical**
(`grid/execution/` ↔ `grid/strategies/` ↔ `ft_user_data/strategies/`).
`tests/test_vendored_sync.py` fails the suite on drift.

## Reliability ledger (M4) — one-minute tour

```sh
# ingest a dry-run instance's tradesv3.sqlite as REAL samples
python3 -m grid.reliability.ledger \
  --sqlite <instance>/tradesv3.sqlite="Long Grid / classic LONG" --report

# research evidence: backtest exports are synthetic seed by default
python3 -m grid.reliability.ledger \
  --backtest-zip <export.zip>=trend_up --out grid/state/reliability.json
```

- Per-archetype buckets keyed by the canonical `ledger_key()` labels —
  identical table to the WT reference (`reliability/wt_reference.py`), so
  the daemon consumes the ledger unchanged.
- `stats` per bucket: samples, synthetic_samples, profit_factor (cap 99),
  recent_pf (last 20), win_rate, expectancy_usd, max_dd_usd,
  gross_profit/loss + grid-native tp/full_exit counts.
- Gates: `size_multiplier()` ladder base 25% → probe ≥10 samples →
  full ≥30 samples & PF ≥1.3; `refuse_new_archetype()` kill gate at
  ≥10 samples & recent_pf < 1.0.
- Archive: `--save-archive` retires a source while keeping its evidence
  (bounded 500/archetype, deduped); `--archive` merges it back in.

Acceptance numbers and the fee-model finding live in
[docs/reliability-ledger.md](docs/reliability-ledger.md).

## Console (web interface)

`console/` is the mission console from the WT-based system (grid-autonomy),
modified in-repo: a stdlib-only HTTP service (`server.py`) that serves the
static UI, reads `grid/state/`, and fail-softs every daemon-proxy call
(the `:8799` ctl plane is down while the legacy daemon stays closed).

Run: `python3 grid/console/server.py` (127.0.0.1:8798; `CONSOLE_PORT` to
override). Serving `:8798` since 2026-09-15 (log: `state/logs/console.log`).

### Execution-engine display

The Fleet page (`#fleet`) shows **which execution engine the fleet runs
on** — the WT-era UI assumed WunderTrading unconditionally:

- `state/engine.json` declares the active engine (`engine`, `mode`,
  `since`, `note`, `legacy: {engine, closed_at, closed_reason}`);
  served by `/api/overview` + `/api/meta` via `engine_payload()`.
- `state/ft_fleet/registry.json` (the M3 freqtrade fleet registry) is
  served **secret-safe** via `ft_fleet_payload()`: instance entries are
  whitelisted field-by-field (`_FT_SAFE_FIELDS`) — the per-instance
  `username`/`password` never cross the API.
- UI: violet **engine banner** at the top of `#fleet` (engine, mode,
  note, freqtrade-fleet counts, legacy closure date), an `engine` chip
  in the statusbar on every page, `Execution engine` + `freqtrade fleet`
  rows in the fleet summary card, and the header subtitle reads
  `mission console · engine: … · daemon: …`.

2026-09-15: the legacy **WunderTrading paper engine was closed**. The
grid-autonomy daemon (launchd `com.tvcli.grid-autonomy`) was halted
through its own ctl plane (`POST /kill` → the daemon armed its own KILL
file, then SIGTERM), and both launchd jobs (`com.tvcli.grid-autonomy`
and `-console`) are `launchctl disable`d so they stay down across
reboots. `grid/state/` is a snapshot of the final WT-era state —
secrets excluded (`llm.env`, `.pocketbase/`, `daemon.pid` never
copied; `config.yaml` copied for the console's Config view). To
re-open the legacy system: `launchctl enable` both labels,
`launchctl bootstrap`/`kickstart` them, and `rm KILL` in grid-autonomy.

## Dev scripts — `grid/dev`

The standalone runtime is managed from the repo itself:

| command | what it does |
|---------|--------------|
| `grid/dev status` | console + engine + registry + ledger at a glance, incl. why-down notes |
| `grid/dev start` | start console (:8798) + the dry-run grid engine (`ft-btc`) |
| `grid/dev stop` | stop both (`--keep-ft` / `--keep-console` to be selective) |
| `grid/dev restart` | stop + start |
| `grid/dev logs [ft\|console] [N]` | tail the engine or console log |
| `grid/dev open` | open the `#fleet` page in a browser |
| `grid/dev ledger` | reliability-ledger summary + refresh hint |

The engine instance is **workspace-native and dry-run only**: it boots
GridStrategy (BTC/USDC:USDC 1h, Hyperliquid public data, M2-tuned
params band_atr 4.2 / step_factor 0.21) from `.venv-ft` into
`state/ft_fleet/ft-btc/` — the same dir layout + registry-entry schema
the M3 backend uses, so the console fleet row reads it natively and the
M3 daemon can adopt it later. Its API port allocates from **8191** (the
M3 backend owns 8091+ — a concurrent M3 smoke can never collide), proxy
variables are stripped from its environment (verified M3 gotcha), and its
per-instance api_server credentials live only in the instance
`config.json` + registry entry — never in logs or the console API.

`grid/dev` deliberately manages **only the engine + console + ledger**:
the autonomy brain (screen → LLM swarm → deploy) is still M3-in-flight in
the grid-autonomy repo and was closed together with the legacy WT engine
(`state/engine.json` records the closure). When M3 lands here, `dev`
grows its brain command.
