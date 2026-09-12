# research-jobs.md — backtest, hyperopt, downloads, background jobs

## Data download first

Backtests and hyperopt need OHLCV (and optionally trades) already on disk.

- `ft_download_data` — POST `/download_data`. `pairs[]` required.
  `timeframes` XOR `days` XOR `timerange` (mutually exclusive — the tool
  rejects combos). `erase: true` wipes existing data for those pairs first;
  `download_trades: true` pulls raw trades; `candle_types` for futures
  (`mark`, `index`, `funding_rate`, `open_interest`, …). Returns a `job_id`;
  poll `ft_background_jobs`.
- One download at a time (Freqtrade enforces this). Check
  `ft_background_jobs` for status/progress/error before starting another.

## Backtest

- `ft_backtest_start` — `strategy` required; data must already exist.
  Optional overrides (`timerange, timeframe, timeframe_detail,
  max_open_trades, stake_amount, dry_run_wallet, backtest_cache,
  enable_protections, freqaimodel, freqai_identifier`). Only one
  backtest/analysis job at a time.
- `ft_backtest_status` — while running: progress/step; when finished: a
  compact per-strategy summary (trades, profit_total, winrate, cagr, sortino,
  sharpe, calmar, profit_factor, max_drawdown). Pass `include_result: true`
  for the full payload (large).
- `ft_backtest_abort` aborts the running job; `ft_backtest_reset` frees cached
  data.
- `ft_backtest_history*` — browse saved results in
  `user_data/backtest_results`: `_history` lists, `_history_get` loads one
  (`filename` + `strategy`, compact unless `include_result`), `_history_notes`
  attaches notes, `_history_delete` removes an entry.

## Hyperopt (CLI — no REST endpoint)

`ft_hyperopt_start` launches `freqtrade hyperopt` in the background:

- `mode` local | ssh | docker (defaults from the instance `host`).
- `docker` requires `container` (the standard `./user_data → /freqtrade/user_data`
  mount) and the freqtrade image with hyperopt deps.
- `local` runs the `freqtrade` binary on this machine (override with
  `freqtrade_bin`); `ssh` runs it on the remote host.
- Logs go to `<user_data>/logs/hyperopt-<name>.log`; a pidfile is written
  (`<user_data>/logs/hyperopt-<name>.pid`) for local/ssh.
- `ft_hyperopt_status` reports liveness (pidfile/`pgrep`), the last log lines
  (`tail_lines`), and the newest files in `user_data/hyperopt_results`
  (`result_files`). `ft_hyperopt_stop` kills it.

Note: hyperopt state is held in the host process memory (`hyperoptJobs`), so a
container restart loses the in-memory job handle — `ft_hyperopt_status` then
falls back to probing the pidfile/log on disk.

## Background jobs (both kinds)

- `ft_background_jobs` — lists backtest and download jobs with status,
  `running`, progress, and error.
- `ft_background_clear` — clears finished jobs (or one `job_id`). Running jobs
  are never removed by the clear form.

## Polling hygiene

Backtest/download jobs can run for many minutes. Poll with
`ft_background_jobs`/`ft_backtest_status` between other work rather than
blocking; abort or clear stragglers once you're done with them so the
instance's job list stays clean.