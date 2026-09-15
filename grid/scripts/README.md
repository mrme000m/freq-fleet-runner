# grid/scripts — operator scripts for the standalone grid fleet

Reusable, well-organized wrappers around `grid/dev` for the recurring
dev processes. Every script is bash 3.2+ compatible (macOS default),
self-contained, and idempotent — safe to run repeatedly and safe to
wire into cron / monitoring.

## Catalog

| Script | Purpose | Idempotent | Side effects |
|--------|---------|------------|--------------|
| `start_all.sh`   | Bring up the full stack (PB + console + 4 engines) | yes | writes PIDs, log files |
| `stop_all.sh`    | Tear down the full stack (or a subset via `--keep-*`) | yes | kills PIDs |
| `restart.sh`     | Stop, wait for ports to clear, start, wait for ping | yes | same as start/stop |
| `prewarm_data.sh`| `freqtrade download-data` for the given exchange/pairs/TFs | yes | writes feathers |
| `screen.sh`      | Run `grid/screen.py` and refresh `screen_cache.json` | yes (refresh) | rewrites cache |
| `health.sh`      | Probe every component, candle-freshness check | yes (read-only) | none |

All scripts share `_lib.sh` (path resolution, ping helper, slot map,
timestamped logging).

## Conventions

- **Run from anywhere.** `_lib.sh` walks up to find `.git` so scripts
  work whether invoked as `./grid/scripts/foo.sh`, `bash …/foo.sh`,
  or from inside `cron` with an absolute path.
- **Exit code = status.** `0` means success, non-zero means something
  failed. `health.sh --quiet` is the only entry point intended for
  alerting; the others log to stdout/stderr.
- **Help is the first 30 lines of each script.** Run any wrapper
  with `--help` to see the docstring without executing anything.
- **macOS bash 3.2 baseline.** No `declare -A`, no `[[ -v name ]]`,
  no `$EPOCHSECONDS`. Every script is smoke-tested with `bash -n`.

## Quick start

```sh
# 1. (one-time per machine) seed OHLCV history for backtests /
#    hyperopt (skipped on Hyperliquid — live engines warm up from
#    per-tick ccxt fetches; use a different exchange for backtests):
./grid/scripts/prewarm_data.sh

# 2. start the full stack:
./grid/scripts/start_all.sh

# 3. (optional) refresh the screen cache (also writes to screen_cache.json):
./grid/scripts/screen.sh

# 4. check everything is healthy:
./grid/scripts/health.sh --json

# 5. open the mission console:
open http://127.0.0.1:8798/#fleet
```

## Lifecycle

```sh
# Full stop (default — kills 4 engines + console + PB):
./grid/scripts/stop_all.sh

# Partial stop (e.g. recycle just the engines, keep console + PB):
./grid/scripts/stop_all.sh --keep-console --keep-pb

# Restart with sanity checks (waits for ports to clear, then for
# engines to respond on /api/v1/ping):
./grid/scripts/restart.sh --wait-secs 30
```

## Prewarm

```sh
# Default: 90 days × {1m, 3m, 5m, 15m} × 4 slot pairs.
# Hyperliquid has no historical OHLCV endpoint via ccxt — the
# script detects that and exits 0 with an explanation. The live
# engines warm up from per-tick ccxt fetches, so no prewarm is
# needed on Hyperliquid.
./grid/scripts/prewarm_data.sh

# Longer history for re-hyperopt:
./grid/scripts/prewarm_data.sh --days 180

# Custom TFs / pairs (useful for verifying the pipeline or for
# backtests on exchanges that do support historical downloads,
# e.g. Binance):
./grid/scripts/prewarm_data.sh --timeframes 1m 5m \
    --exchange binance --pairs BTC/USDT:USDT
```

`prewarm_data.sh` uses freqtrade's `--prepend` flag when supported, so
re-running it daily only fetches the missing tail. The script prints
which feather files landed and exits non-zero only when an exchange
that *should* support downloads (Binance, Bybit, OKX, …) wrote zero
files. On Hyperliquid it always exits 0 — engines there run on live
ticks only.

## Screen

```sh
# Default top-30 ranked universe (24h vol × 5m ATR%, post-2026-09-15
# reset):
./grid/scripts/screen.sh

# Wider universe, fewer candle fetches (faster, less coverage):
./grid/scripts/screen.sh --top 50 --fetch 10

# Custom cache location (diff-friendly):
./grid/scripts/screen.sh --out /tmp/screen-$(date +%s).json
```

`grid/screen.py` is a stdlib-only Hyperliquid client; it works
offline as long as the public HL info endpoint is reachable.

## Health probe

```sh
# Human-readable (default — ok/DOWN/stale per component):
./grid/scripts/health.sh

# Machine-readable JSON for monitoring:
./grid/scripts/health.sh --json

# Exit code only (suitable for cron → alerting):
./grid/scripts/health.sh --quiet && echo OK || echo DEGRADED
```

The probe checks:
1. **PocketBase** on `:8290` via `/api/health`
2. **Mission console** on `:8798` via `/`
3. **Each engine** on `:8191`-`:8194` via `/api/v1/ping`
4. **Candle freshness** per slot — the last bar from the engine's
   `/api/v1/pair_candles?timeframe=<slot_tf>&limit=1` must be within
   `3 × TF` seconds. A `1m` slot is stale after `180s`; a `5m` slot
   after `900s`.

Exit code is `0` only when every component passes both ping and
freshness. Wire into cron:

```cron
*/5 * * * * /Volumes/ExMac/code/grid/0/freqtrade/grid/scripts/health.sh --quiet \
    || /usr/local/bin/notify-grid-ops "grid fleet degraded"
```

## Adding a new script

1. Start from the existing patterns: `set -euo pipefail`, source
   `_lib.sh`, log with `log_info` / `log_warn` / `log_err`.
2. Read `--help` from the first 30 lines so `head -30` always shows
   the docstring.
3. End every script with `exit $?` (or `exit <n>`) so cron sees the
   real status — never rely on the last command's exit.
4. Bash 3.2 only. Use a `mktemp` directory for state instead of
   `declare -A` (see `health.sh`).
5. Update this README's catalog table when you add a row.
