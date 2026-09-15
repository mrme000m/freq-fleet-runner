# freq-fleet-runner — autonomous grid trading on freqtrade

A **standalone** repo: everything needed to research, validate, and run the
autonomous grid-trading system's freqtrade leg lives here. The agent layer
(dash container) deploys and manages fleets of
[Freqtrade](https://github.com/freqtrade/freqtrade) instances; the `grid/`
tree holds the grid-trading core itself.

Layout — everything is committed except the local scratch noted below:

```
grid/                       the grid-trading core (standalone)
├── execution/grid_geometry.py   pure geometry: ATR channel, geometric lines,
│                                fee-floor step, per-line sizing
├── strategies/                  GridStrategy.py (+ M2-tuned .json) and a
│                                vendored copy of grid_geometry.py
├── reliability/                 M4: per-archetype reliability ledger on
│                                freqtrade SQLite order-tag round-trip
│                                pairing (ledger.py, pairing.py;
│                                wt_reference.py = frozen WunderTrading
│                                reference the shape mirrors)
├── console/                    mission console (web interface, copied
│                                from the WT-based system; wired to the
│                                freqtrade fleet at M5)
├── docs/                        roadmap + M4 design/acceptance records
├── tools/                       m1_reconcile.py, crossval_step.py
└── tests/                       geometry (42), M4 ledger, vendored-sync,
                                 and M4 acceptance (runs where the
                                 research scratch exists)

freqtrade-fleet-manager/    the dsh plugin: 56 ft_* tools (registry,
                            lifecycle, telemetry, secrets, config,
                            deploy, backtest, hyperopt, download) + web
                            dashboard
docker/dsh-ffm/             the agent container (dsh web :3081, ffm
                            preset, vault loader, PIN gate) — also the
                            M6 host for the remote fleet
.github/workflows/          dsh-ffm-deploy.yml, fleet-runner.yml
client/index.js             plugin client entry (mirrors lib/client.js)
```

Local-only scratch (gitignored, this machine): `freqtrade/` (upstream
clone, reference), `ft_user_data/` (freqtrade user_data: research data,
backtest/hyperopt results, the runtime strategy copies), `.venv-ft/`
(local freqtrade install). The M3 daemon (in the companion grid-autonomy
repo) points at `.venv-ft/bin` and `ft_user_data/strategies` — those
paths are load-bearing and must not move.

## Status (milestones in grid/docs/freqtrade-execution-engine.md)

| Milestone | State |
|---|---|
| M0 local freqtrade env | done 2026-09-14 |
| M1 GridStrategy spike + backtest + reconcile invariants | done 2026-09-14 |
| M2 hyperopt (band_atr 4.2, step_factor 0.21; +0.99%, 127/127 tp trips) | done 2026-09-14 |
| M3 dry-run loop wired to the agent layer | in flight (companion repo; freqtrade_backend.py seam + unit tests) |
| M4 reliability ledger on SQLite order-tag pairing | done here (grid/reliability) |
| M5 multi-instance paper fleet | next |
| M6 remote/live + containerized | later; the dsh-ffm container is already self-deploying |

## Quickstart

```sh
# run + manage the standalone system (console :8798 + dry-run grid engine)
grid/dev status              # everything at a glance (incl. why-down notes)
grid/dev start               # console + freqtrade dry-run engine (ft-btc)
grid/dev stop                # stop both (restart / logs / open / ledger too)

# grid core tests (pure stdlib; python3 with pytest)
python3 -m pytest grid/tests/ -q

# reliability ledger — ingest a dry-run instance's SQLite (real samples)
python3 -m grid.reliability.ledger \
    --sqlite ft-instance-dir/tradesv3.sqlite="Long Grid / classic LONG" \
    --report

# ...or research evidence (backtest exports stay synthetic seed — they
# never gate the sizing ladder unless forced with !!real)
python3 -m grid.reliability.ledger \
    --backtest-zip ft_user_data/backtest_results/m1_grid.zip=trend_up \
    --report

# plugin build / verify
cd freqtrade-fleet-manager && pnpm install --frozen-lockfile && pnpm run verify

# container build + run (see docker/dsh-ffm/README.md)
docker build -f docker/dsh-ffm/Dockerfile -t dsh-ffm:local .
```

See `grid/README.md` for the core's details,
`grid/docs/reliability-ledger.md` for the M4 design + acceptance
numbers, `freqtrade-fleet-manager/README.md` for the plugin tool
catalog, and `docker/dsh-ffm/README.md` for the container and its
self-modification round-trip.
