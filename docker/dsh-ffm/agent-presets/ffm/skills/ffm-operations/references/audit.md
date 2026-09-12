# audit.md — periodic health sweep, posture, PnL

Run this on a schedule (or before/after any change window). One call covers
most of it: `ft_fleet_overview` pings every instance in parallel and, for
reachable ones, collects open-trade count and realized PnL. Then drill into
anything it flags.

## 1. Health sweep

`ft_instances_list` → `ft_fleet_overview`. Read `up`, `latency_ms`,
`ping_error` per instance:

- `up: false` + `ping_error: 'no base_url'` → registry entry missing
  `base_url` — fix with `ft_instances_update`.
- `ping_error: 'HTTP 4xx/5xx'` → API up but misconfigured (wrong port,
  auth off, `listen_ip_address` not reachable) — check `ft_show_config`.
- High `latency_ms` consistently → network/tunnel issue.

## 2. Fleet posture

For each instance, `ft_status` (open trades) + `ft_show_config`:

- confirm `dry_run` is what you intend (the fleet default is `dry_run: true`).
- confirm `strategy` / `exchange` / `state` match expectations.
- flag any instance with `state` ≠ expected or unexpectedly many/few open
  trades.

`ft_fleet_overview` already folds the open-trade count and `trading` flag into
the sweep; use per-instance `ft_status` only when you need the full trade list.

## 3. PnL read

`ft_profit` (realized/unrealized + trade count) and `ft_balance` (free/used
per currency). The overview's `realized{profit_ratio, profit_abs,
profit_currency, trade_count}` is the compact form; pull `ft_profit` for the
unrealized split when the realized number moves.

## 4. Config drift

`ft_show_config` vs `ft_config_validate`. Fix schema/validation errors; watch
for redacted secrets (manage them via `ft_secret_*`, not the config file).

## 5. Jobs

`ft_background_jobs` per instance: no stuck backtests/downloads. Clear or
abort stragglers (`ft_background_clear`, `ft_backtest_abort`).

## 6. Topology

Confirm producer→consumer links: `ft_show_config` →
`external_message_consumer` block on each consumer (see
`references/topology.md`). A broken link = missing `ws_token`, wrong host/port,
or `enabled: false`.

## Summary format

| Instance | up | latency | open | realized | dry_run | notes |
|---|---|---|---|---|---|---|
| freq-fleet | ✅ | 42ms | 2 | +1.2% | true | — |
| remote-1 | ❌ HTTP 401 | — | — | — | true | auth token stale → `ft_secret_set api_password` |