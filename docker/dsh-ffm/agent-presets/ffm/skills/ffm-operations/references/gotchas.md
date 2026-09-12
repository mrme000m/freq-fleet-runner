# gotchas.md — edge cases and footguns

## dry_run vs live

- **`dry_run: true` is the safe posture.** The fleet's deployed instance
  (`freq-fleet`) runs dry-run; keep new instances dry-run by default.
- `ft_instances_add` defaults `dry_run` to `true`; `ft_instances_update` is the
  in-place way to flip it, and it warns when you set `dry_run: false`.
- Lift `dry_run` only on explicit human confirmation. A live bot places real
  orders; `ft_forceenter`/`ft_forceexit`/`ft_blacklist_*`/`ft_lock_*` then
  affect real capital.

## Transport

- **`web.fetch` is GET-only.** Control calls need `curl` — that is why every
  authed tool shells out (JSON body on stdin, HTTP status from a `-w` trailer).
  Do not try to POST through the web tool.
- JWT is per call (`POST /api/v1/token/login`); tokens are never cached or
  written to disk.
- Only `/api/v1/ping` is public; every other endpoint requires `api_username`
  + `api_password`.

## Secrets

- Secrets live in the host `credentials` service under `FTMGR_<NAME>_<KIND>`
  and are resolved per call — never echoed by read tools.
- `ft_show_config` may redact secrets (masked values) — use `ft_secret_*` to
  manage them, not the config file.
- `ft_instances_remove` deletes the registry entry but leaves the credential
  refs in place (safe for re-add; delete with `ft_secret_unset` if you want a
  clean removal).
- Never put the git push token (`GH_FLEET_TOKEN`) in a log, commit, or tool
  result — it flows through the askpass helper only.

## Docker deploys

- Docker-mode deploys need the docker CLI/socket mounted on this host (see
  `docker/dsh-ffm/README.md`). `ft_deploy_local`/`ft_deploy_ssh` write
  `config.json` + `docker-compose.yml` and run `docker compose`.
- `config.json` is regenerated fresh on each deploy (it overwrites) — keep the
  source of truth in the registry + secrets, not in a hand-edited file on the
  deploy host.

## Registry

- The registry file (`/data/dsh/.freqtrade-instances.json`) is the source of
  truth — the dashboard and the tools read it directly; don't keep a second
  copy elsewhere.
- `name` is immutable (it keys secrets and configs). Rename = remove + re-add
  (then re-set secrets under the new name).

## Hyperopt

- Hyperopt has no REST endpoint — it's a `nohup` + pidfile background process
  in local/ssh/docker. The in-memory job handle does not survive a container
  restart; `ft_hyperopt_status` falls back to probing the pidfile/log.
- Docker hyperopt needs the freqtrade image with hyperopt deps.

## Fleet overview

- `ft_fleet_overview` never fails the whole sweep on a bad instance — read
  `ping_error`/`status_error`/`profit_error` per row. `up: true` only means
  `/ping` answered 2xx; `status_error` can still occur if auth is stale.