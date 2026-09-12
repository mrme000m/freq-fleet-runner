# tools.md — the full `ft_*` surface

Exact parameters live in each tool's schema; this is the operator's reference
for payload shapes and behavior that isn't obvious from the name. All authed
calls take `name` first unless noted.

## Registry

- `ft_instances_add` — register `name, base_url, api_username, api_password,
  host (local|ssh), ssh_target, user_data, strategy, exchange, dry_run`.
  `name` must match `[a-z0-9][a-z0-9_-]{0,63}`. `api_password` is stored in the
  credentials service as `FTMGR_<NAME>_API_PASSWORD` when available, else inline
  (never echoed). Returns the redacted instance.
- `ft_instances_update` — mutate `base_url, api_username, api_password,
  host, ssh_target, user_data, strategy, exchange, dry_run` in place. `name` is
  immutable (remove + re-add to rename). `api_password` rotates the credential
  ref. Returns `updated` (list of changed fields) + redacted instance + a
  `note` that warns when `dry_run=false` or when `host=ssh` lacks `ssh_target`.
- `ft_instances_list` — all instances, secrets redacted (`api_password_set`,
  `credential_ref` instead of the value), plus `registryFile`.
- `ft_instances_get` / `ft_instances_remove` — single instance / delete.

## Fleet sweep

- `ft_fleet_overview` — one-call read-only sweep. Pings every instance in
  parallel (cap 4), then for each reachable instance fetches `GET /status`
  (open-trade count + summed `profit_ratio`) and, unless
  `include_profit: false`, `GET /profit` (realized). Returns `{ count, up,
  down, open_trades_total, instances: [ { name, base_url, host, strategy,
  exchange, dry_run, up, latency_ms, ping_error, trading, open_trades,
  open_profit_ratio, status_error, realized{…}, profit_error } ] }`.
  `ping_timeout_ms` defaults to 6000 (max 20000). Per-instance errors never
  fail the sweep — they surface as `ping_error`/`status_error`/`profit_error`.

## Passthrough

- `ft_api` — `method` GET|POST|DELETE, `path` (must start with `/`), optional
  JSON `body`. Auto-JWT; `/ping` skips auth.

## Lifecycle (POST) — `ft_start` `ft_stop` `ft_pause` `ft_stopbuy` `ft_reload_config`

`ft_stop` stops the bot; `ft_pause` gracefully handles open trades and stops
new entries; `ft_stopbuy` stops entries and closes open trades; `ft_reload_config`
reloads the config file (no restart).

## Telemetry (GET)

- `ft_ping` — unauthenticated readiness probe.
- `ft_version` `ft_health` `ft_sysinfo` — version / last bot loop / load.
- `ft_status` — open trades array (`{trading: bool, trades: [...]}`).
- `ft_balance` / `ft_profit` / `ft_performance` — balances per currency /
  realized+unrealized PnL / finished-trade PnL by pair.
- `ft_whitelist` / `ft_blacklist` / `ft_locks` / `ft_trades` — pairlists,
  locks, recent trades (≤500).
- `ft_show_config` — current config (may redact secrets). `ft_logs` — tail.

## Mutation

- `ft_blacklist_add` / `ft_blacklist_delete` — `pairs` is a comma-separated
  string; POST/DELETE `/blacklist` with `{blacklist: [...]}`.
- `ft_forceenter` — `pair` required; `side` long|short, `price` (limit),
  `stake_amount` (→ `stakeamount`), `ordertype` market|limit, `entry_tag`.
  Requires `force_entry_enable: true` on the instance.
- `ft_forceexit` — `tradeid` (or `"all"`), `ordertype`, `amount`.
- `ft_lock_add` — `pair`, `until` (ISO datetime, rounded to timeframe),
  `side`, `reason`. `ft_lock_delete` — `lock_id`.

## Secrets (credential-backed)

- `ft_secret_set` — `secret` ∈ `api_password | ws_token | ssh_key |
  ssh_key_passphrase | telegram_token | jwt_secret_key`, non-empty `value`.
  Stored as `FTMGR_<NAME>_<KIND>`. `api_password` also updates the registry
  ref used for login.
- `ft_secret_unset` — remove a secret kind.
- `ft_secret_status` — per-kind `{configured, writable, source, ref}` — values
  never exposed.

## Config

- `ft_config_validate` — `config` (JSON object), `runmode`
  (other|dry_run|live|backtest|hyperopt|webserver), `strict` (unknown top-level
  keys become errors instead of warnings). Structural lint only — no exchange
  or strategy resolution. Returns `valid`, `errors[]`, `warnings[]`.
- `ft_config_generate` — build a starter `config.json` from registry fields
  (dry_run, api_server port derived from `base_url`, exchange, strategy,
  api_server credentials + jwt). `overrides` deep-merge; `write: true` +
  `target_path` writes via the fs service.

## Backtest / data / background

- `ft_backtest_start` — POST `/backtest`; `strategy` required. Optional
  `timerange, timeframe, timeframe_detail, max_open_trades, stake_amount,
  dry_run_wallet, backtest_cache, enable_protections, freqaimodel,
  freqai_identifier`. One backtest at a time; OHLCV must already be downloaded.
- `ft_backtest_status` — GET `/backtest`; running → progress/step, finished →
  compact per-strategy summary unless `include_result: true`.
- `ft_backtest_abort` / `ft_backtest_reset` — abort / free cached data.
- `ft_backtest_history*` — list/get/delete/notes for `user_data/backtest_results`.
- `ft_background_jobs` / `ft_background_clear` — list backtest+download jobs;
  clear finished (or one `job_id`).
- `ft_download_data` — POST `/download_data`; `pairs[]` required. `timeframes`
  XOR `days` XOR `timerange`. Optional `erase, download_trades, candle_types,
  prepend_data, trading_mode, margin_mode, exchange`.

## Hyperopt (CLI, no REST surface)

- `ft_hyperopt_start` — `mode` local|ssh|docker (defaults from `host`). Options
  mirror the `freqtrade hyperopt` CLI (`epochs, strategy, hyperopt_loss,
  spaces, timerange, timeframe, job_workers, min_trades, random_state,
  early_stop, print_json, print_all, extra_args`). Logs to
  `<user_data>/logs/hyperopt-<name>.log`; `docker` needs `container`.
- `ft_hyperopt_status` — liveness (pidfile / `pgrep` in docker), log tail,
  newest `hyperopt_results`. `ft_hyperopt_stop` — kill pid / `pkill` in docker.

## Topology

- `ft_instance_link_producer` — see `references/topology.md`.