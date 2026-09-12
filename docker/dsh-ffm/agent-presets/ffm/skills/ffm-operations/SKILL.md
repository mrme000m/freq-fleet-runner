---
name: ffm-operations
description: Operate, audit, and improve the Freqtrade fleet from the dsh-ffm agent — the instance registry at /data/dsh/.freqtrade-instances.json, the 54 ft_* tools (REST transport via curl with per-call JWT, secrets via the host credentials service under FTMGR_<NAME>_<KIND> refs), local + SSH deploy/sync/bootstrap flows, backtest/hyperopt/download background jobs, producer/consumer topology, and the plan-capacity gotchas (dry_run vs live). Load before managing, deploying, or configuring any Freqtrade instance.
---

# ffm-operations — the Freqtrade fleet owner's playbook

You are the operator of the Freqtrade fleet, served by this dsh-ffm agent.
This is the concise playbook for MONITOR → MANAGE → DEPLOY. The full tool
surface is the freqtrade-fleet-manager bundle itself (`lib/index.js` in the
installed package); trust its tool schemas over this summary when they
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

## The 54-tool surface (grouped)

| Group | Tools |
|---|---|
| Registry | `ft_instances_add` `ft_instances_list` `ft_instances_get` `ft_instances_remove` |
| Passthrough | `ft_api` (any `/api/v1` endpoint, auto-JWT) |
| Lifecycle (POST) | `ft_start` `ft_stop` `ft_pause` `ft_stopbuy` `ft_reload_config` |
| Telemetry (GET) | `ft_ping` `ft_version` `ft_health` `ft_sysinfo` `ft_status` `ft_balance` `ft_profit` `ft_performance` `ft_whitelist` `ft_blacklist` `ft_locks` `ft_trades` `ft_show_config` `ft_logs` |
| Mutation | `ft_blacklist_add` `ft_blacklist_delete` `ft_forceenter` `ft_forceexit` `ft_lock_add` `ft_lock_delete` |
| Secrets | `ft_secret_set` `ft_secret_unset` `ft_secret_status` (kinds: `api_password`, `ws_token`, `ssh_key`, `ssh_key_passphrase`, `telegram_token`, `jwt_secret_key`) |
| Config | `ft_config_validate` `ft_config_generate` (against the distilled `config_schema`) |
| Deploy / sync | `ft_deploy_local` `ft_deploy_ssh` `ft_sync_user_data` `ft_bootstrap_host` |
| Backtest / data | `ft_backtest_start` `_status` `_abort` `_reset` `_history` `_history_get` `_history_delete` `_history_notes`, `ft_download_data`, `ft_background_jobs` `ft_background_clear` |
| Hyperopt (CLI) | `ft_hyperopt_start` `_status` `_stop` (local / ssh / docker; `nohup`+pidfile — no REST endpoint) |
| Topology | `ft_instance_link_producer` (writes `external_message_consumer.producers` into a consumer's config) |

## Operating rules

1. **Inspect before you act.** `ft_instances_list` → `ft_ping` (public
   liveness) → `ft_status` / `ft_show_config` before touching an instance.
   `ft_status` shows `state` (running/stopped), `ft_balance`/`ft_profit` the
   PnL read.
2. **Secrets stay in `credentials`.** Prefer `ft_secret_set` refs
   (`FTMGR_<NAME>_<KIND>`) over passwords in the registry; `ft_instances_*`
   never print them. Never echo a secret in a tool result or log.
3. **Destructive actions are deliberate.** `ft_forceenter`, `ft_forceexit`,
   `ft_blacklist_*`, `ft_lock_*`, `ft_stop`, `ft_pause`, `ft_stopbuy` change
   live state — confirm the target and effect before calling.
4. **Deploy paths.** `ft_deploy_local` / `ft_deploy_ssh` use Docker Compose
   (the plugin writes a `docker-compose.yml` + `config.json` under
   `user_data`); `ft_bootstrap_host` provisions a remote host; `ft_sync_user_data`
   rsyncs a local `user_data` tree to a remote instance. `ssh_target` is
   `user@host:port`; key-based auth only, key from `credentials`.
5. **Background jobs.** Backtest / download run through Freqtrade's own REST
   background-job routes (`/backtest`, `/download_data`, `/background`);
   `ft_background_jobs` shows them, `ft_background_clear` cancels. Hyperopt
   is a CLI background process (`nohup`+pidfile).
6. **Multi-instance.** Link a producer→consumer with `ft_instance_link_producer`
   (shares `analyzed_df`/whitelist over the message WebSocket with
   `ws_token`). Remote instances must bind `api_server.listen_ip_address` to
   `0.0.0.0` (or be reached through an SSH tunnel) for the agent to reach them.

## Gotchas

- **`dry_run: true` is the safe posture.** The fleet's deployed instance
  (`freq-fleet`) runs `dry_run: true`; keep new instances dry-run by default
  and only lift `dry_run` on explicit human confirmation.
- **`web.fetch` is GET-only.** Control calls need curl — that is why the
  tools shell out; do not try to POST through the web tool.
- **`ft_show_config` may redact secrets.** Passwords/keys in an instance's
  config are masked; use `ft_secret_*` to manage them.
- **Docker mode deploys need the docker CLI/socket on this host.** For
  `host: docker` flows, the container must be launched with the docker
  socket mounted (see `docker/dsh-ffm/README.md`).
- **The registry file is the source of truth.** The dashboard and the tools
  read it directly; do not keep a second copy elsewhere.

## Periodic audit checklist

1. **Health sweep** — `ft_instances_list` → `ft_ping` each; flag any 2xx
   failure or latency spike.
2. **Fleet posture** — `ft_status` per instance: running/stopped, exchange,
   strategy, `dry_run` flags, `state` (trade/fill).
3. **PnL read** — `ft_profit` (realized/unrealized + trade count) and
   `ft_balance` (free/used) per instance.
4. **Config drift** — `ft_show_config` vs `ft_config_validate`; fix
   schema/validation errors.
5. **Jobs** — `ft_background_jobs`: no stuck backtests/downloads; clear or
   abort stragglers.
6. **Topology** — confirm producer→consumer links (`ft_instance_link_producer`
   writes, `ft_show_config` verifies the `external_message_consumer` block).