---
name: ffm-operations
description: Operate, audit, and improve the Freqtrade fleet from the dsh-ffm agent — the instance registry at /data/dsh/.freqtrade-instances.json, the 56 ft_* tools (REST transport via curl with per-call JWT, secrets via the host credentials service under FTMGR_<NAME>_<KIND> refs), local + SSH deploy/sync/bootstrap flows, backtest/hyperopt/download background jobs, producer/consumer topology, and the plan-capacity gotchas (dry_run vs live). Load before managing, deploying, or configuring any Freqtrade instance.
---

# ffm-operations — the Freqtrade fleet owner's playbook

You are the operator of the Freqtrade fleet, served by this dsh-ffm agent.
This is the concise playbook for **MONITOR → MANAGE → DEPLOY**. It is
progressively discoverable: read this file first, then load only the
`references/*.md` file your current task needs (see the map below). The full
tool surface is the freqtrade-fleet-manager bundle itself (`lib/index.js` in
the installed package); trust a tool's schema over any summary when they
disagree.

## Fleet layout (read this first)

- **The registry** — `/data/dsh/.freqtrade-instances.json` (persistent
  `ffm-dsh` volume). One object per instance:
  `name, base_url, username, password|password_ref, host (local|ssh),
  ssh_target, user_data, strategy, exchange, dry_run, created_at, updated_at`.
  Secrets are NEVER echoed back: `ft_instances_list/get` return
  `api_password_set: true`, never the value. Prefer `ft_secret_set` /
  `ft_secret_status` over inline passwords.
- **REST transport** — every authed call shells out to `curl` with the JSON
  body on stdin and the HTTP status recovered from a `-w` trailer; JWT is
  obtained per call from `POST {base_url}/api/v1/token/login`. Only
  `/api/v1/ping` is public. `web.fetch` is GET-only — never use it for the
  control API.
- **The dashboard** — Settings → Freqtrade Fleet (this deployment's own web
  UI), fed by `GET /freqtrade/api/fleet?ping=1` on the host web server.

## Operating rules

1. **Inspect before you act.** `ft_instances_list` → `ft_fleet_overview` (or
   `ft_ping` per instance) → `ft_status` / `ft_show_config` before touching an
   instance. `ft_status` shows open trades; `ft_balance`/`ft_profit` the PnL.
2. **Secrets stay in `credentials`.** Prefer `ft_secret_set` refs
   (`FTMGR_<NAME>_<KIND>`) over passwords in the registry; `ft_instances_*`
   never print them. Never echo a secret in a tool result or log.
3. **Destructive actions are deliberate.** `ft_forceenter`, `ft_forceexit`,
   `ft_blacklist_*`, `ft_lock_*`, `ft_stop`, `ft_pause`, `ft_stopbuy` change
   live state — confirm the target and effect before calling.
4. **Registry edits are in-place.** Use `ft_instances_update` to change
   `strategy` / `exchange` / `dry_run` / `ssh_target` / `user_data` /
   `base_url` / `api_password` without remove + re-add. `name` is immutable.
5. **Background jobs.** Backtest / download run through Freqtrade's own REST
   background-job routes (`/backtest`, `/download_data`, `/background`);
   `ft_background_jobs` shows them, `ft_background_clear` cancels. Hyperopt
   is a CLI background process (`nohup` + pidfile).

## The 56-tool surface (grouped)

| Group | Tools |
|---|---|
| Registry | `ft_instances_add` `ft_instances_update` `ft_instances_list` `ft_instances_get` `ft_instances_remove` |
| Fleet sweep | `ft_fleet_overview` (parallel ping + open trades + PnL for all instances) |
| Passthrough | `ft_api` (any `/api/v1` endpoint, auto-JWT) |
| Lifecycle (POST) | `ft_start` `ft_stop` `ft_pause` `ft_stopbuy` `ft_reload_config` |
| Telemetry (GET) | `ft_ping` `ft_version` `ft_health` `ft_sysinfo` `ft_status` `ft_balance` `ft_profit` `ft_performance` `ft_whitelist` `ft_blacklist` `ft_locks` `ft_trades` `ft_show_config` `ft_logs` |
| Mutation | `ft_blacklist_add` `ft_blacklist_delete` `ft_forceenter` `ft_forceexit` `ft_lock_add` `ft_lock_delete` |
| Secrets | `ft_secret_set` `ft_secret_unset` `ft_secret_status` (kinds: `api_password`, `ws_token`, `ssh_key`, `ssh_key_passphrase`, `telegram_token`, `jwt_secret_key`) |
| Config | `ft_config_validate` `ft_config_generate` |
| Deploy / sync | `ft_deploy_local` `ft_deploy_ssh` `ft_sync_user_data` `ft_bootstrap_host` |
| Backtest / data | `ft_backtest_start` `_status` `_abort` `_reset` `_history` `_history_get` `_history_delete` `_history_notes`, `ft_download_data`, `ft_background_jobs` `ft_background_clear` |
| Hyperopt (CLI) | `ft_hyperopt_start` `_status` `_stop` (local / ssh / docker; `nohup` + pidfile — no REST endpoint) |
| Topology | `ft_instance_link_producer` (writes `external_message_consumer.producers` into a consumer's config) |

## Progressive disclosure map

Load the reference only when the task needs it — don't pull all of them into
context for a routine call.

| Task | Reference |
|---|---|
| Any single tool's exact parameters / payload shape | `references/tools.md` |
| Local or SSH deploy, rsync sync, remote host bootstrap | `references/deploy-flows.md` |
| Backtest, hyperopt, OHLCV/trades download, background jobs | `references/research-jobs.md` |
| Producer→consumer linking, remote API reachability | `references/topology.md` |
| Periodic health sweep / posture / PnL audit | `references/audit.md` |
| Editing this agent's own code, rebuild, commit/push, redeploy | `references/self-modification.md` |
| Edge cases and footguns (dry_run vs live, redaction, docker socket) | `references/gotchas.md` |