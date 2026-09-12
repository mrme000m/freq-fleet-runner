# freqtrade-fleet-manager

Deploy, manage, and configure multiple [Freqtrade](https://github.com/freqtrade/freqtrade) instances — local (bare metal / Docker / systemd) and remote (SSH) — from a [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`) agent, in one installable plugin package.

- **Host engine**: the instance registry (persisted to `$DSH_HOME/.freqtrade-instances.json`), a **54-tool** `ft_*` control surface, credential-backed secrets, config validation/generation, local + SSH deploy/sync/bootstrap, and the `/freqtrade` JSON API for the dashboard.
- **Model-facing tools**: registered into the host `tools` registry from the bundle row, so every session (any preset) sees them — no preset setup required.
- **Web UI**: a Settings → **Freqtrade Fleet** section listing registered instances with live unauthenticated `/ping` health and latency.

## Install

Requires the `dsh` CLI (`@deepseek-ai/dsh`) on the host, `curl`, and (for remote deploys) `ssh`/`scp`/`rsync` on PATH.

```sh
# from npm (when published)
dsh plugin --profile web add freqtrade-fleet-manager

# from a git checkout
dsh plugin --profile web add github:<owner>/freqtrade-fleet-manager

# from a local checkout (ships the current lib/ as-is)
dsh plugin --profile web add ./path/to/freqtrade-fleet-manager
```

Then restart the profile (`dsh web`) so the bundle patch merges and the host row mounts. The tools appear in every session's catalog; the dashboard appears under Settings.

## Compatibility

| dsh | supported |
| --- | --- |
| 0.1.0-rc.7, 0.1.1-rc.2 (npm `latest`) | ✅ |
| 0.1.0-rc.5 and older | ❌ |

The dsh-family packages are declared as peer dependencies with exact version chains, resolved at runtime from the running dsh installation (dsh materializes module-fallback links into `$DSH_HOME/profiles/node_modules`), so the plugin shares the host's module instances instead of installing duplicates.

## Package layout

One package, two mounted surfaces:

| Surface | Mount | Content |
| --- | --- | --- |
| `exports "."` | bundle row `freqtrade-fleet-manager` (from `cordis.patch.yml`) | host engine: registry + 54 tools + `/freqtrade` API |
| `exports "./client"` (`dsh.client`) | browser roster (scanned from mounted entries) | Settings → Freqtrade Fleet dashboard |

No agent preset is needed: the tools register into the host `tools` registry from the host-plane bundle row, so they are available to every session.

## Tool catalog

- **Registry**: `ft_instances_add` / `ft_instances_list` / `ft_instances_get` / `ft_instances_remove`
- **Generic passthrough**: `ft_api` (any `/api/v1` endpoint, auto-JWT)
- **Lifecycle**: `ft_start` `ft_stop` `ft_pause` `ft_stopbuy` `ft_reload_config`
- **Telemetry**: `ft_ping` `ft_version` `ft_health` `ft_sysinfo` `ft_status` `ft_balance` `ft_profit` `ft_performance` `ft_whitelist` `ft_blacklist` `ft_locks` `ft_trades` `ft_show_config` `ft_logs`
- **Mutation**: `ft_blacklist_add` `ft_blacklist_delete` `ft_forceenter` `ft_forceexit` `ft_lock_add` `ft_lock_delete`
- **Secrets** (credential-backed, refs `FTMGR_<NAME>_<KIND>`): `ft_secret_set` / `ft_secret_unset` / `ft_secret_status` (kinds: `api_password`, `ws_token`, `ssh_key`, `ssh_key_passphrase`, `telegram_token`, `jwt_secret_key`)
- **Config**: `ft_config_validate` `ft_config_generate`
- **Deploy / sync / bootstrap**: `ft_deploy_local` `ft_deploy_ssh` `ft_sync_user_data` `ft_bootstrap_host`
- **Backtest**: `ft_backtest_start` `ft_backtest_status` `ft_backtest_abort` `ft_backtest_reset` `ft_backtest_history` `ft_backtest_history_get` `ft_backtest_history_delete` `ft_backtest_history_notes`
- **Background jobs / data**: `ft_background_jobs` `ft_background_clear` `ft_download_data`
- **Hyperopt** (CLI; local/ssh/docker): `ft_hyperopt_start` `ft_hyperopt_status` `ft_hyperopt_stop`
- **Producer/consumer topology**: `ft_instance_link_producer`

## Architecture notes

- **REST transport**: `web.fetch` is GET-only, so authed control calls shell out to `curl` with the JSON body on stdin and the HTTP status recovered from a `-w` trailer. JWT is obtained per call from `POST /api/v1/token/login`; only `/ping` is public.
- **Secrets**: API password / SSH key / `ws_token` / JWT secret live in the host `credentials` service under POSIX-env-style refs and are resolved per call — never echoed by read tools.
- **Backtest / download**: driven through Freqtrade's own REST background-job routes (`/backtest`, `/download_data`, `/background`).
- **Hyperopt**: no REST run endpoint exists, so it runs the `freqtrade hyperopt` CLI as a background process (`nohup` + pidfile) in `local` / `ssh` / `docker` modes.
- **Dashboard RPC**: the client fetches `GET /freqtrade/api/fleet?ping=1` over plain HTTP, served by the host entry through the `webServer` service — no per-package host-method channel needed.
- **Tool results** are passed through a recursive `clean()` pass that strips `undefined`/functions/symbols and maps `NaN`/`Infinity` to `null`, satisfying the lossless-JSON tool-result constraint.

## Development

```sh
pnpm install
pnpm run build      # lib/index.js (ESM host) + lib/client.js (browser closure factory)
pnpm run verify     # build + npm pack --dry-run
```

- `src/index.js` — host entry (registry, helpers, 54 tools, `/freqtrade` API).
- `client/index.js` — browser half (Settings → Freqtrade Fleet).
- `cordis.patch.yml` — the bundle row the profile boot merges.
- `tsdown.config.mjs` — host ESM build (peers external) + browser CJS closure-factory build.

## Uninstall

```sh
dsh plugin --profile web remove freqtrade-fleet-manager
```

The registry file (`$DSH_HOME/.freqtrade-instances.json`) and any credentials you set are left in place; delete them yourself if you want a clean removal.

## License

MIT