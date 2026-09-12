# deploy-flows.md — local & SSH deploy, sync, bootstrap

Everything here shells out to `ssh`/`scp`/`rsync`/`docker` from this host.
Key-based auth only for SSH; the key comes from the credentials service
(`ft_secret_set <name> ssh_key …`). **Dry-run first** (`dry_run: true` is the
default on every deploy/sync/bootstrap tool) — read the returned plan, then
re-run with `dry_run: false`.

## Local deploy — `ft_deploy_local`

1. Writes `config.json` (generated from the registry, or your `config` arg)
   into `<deploy_root>/user_data/config.json` and a `docker-compose.yml` into
   `<deploy_root>/`.
2. Runs `docker compose up -d` (or `down`/`restart`/`ps`).
3. `deploy_root` defaults to the parent of the instance `user_data`. The
   compose file mounts `./user_data:/freqtrade/user_data` and maps
   `127.0.0.1:<port>:<port>`; `<port>` comes from `config.api_server.listen_port`
   or is parsed from the instance `base_url`.

Gotchas: the container needs the docker CLI/socket mounted (see
`references/gotchas.md`); `config.json` is written fresh each run (it
overwrites — keep your real config in the repo/registry, not in the file).

## Remote deploy — `ft_deploy_ssh`

Same as local, but: stage files under
`$WORKSPACE/.freqtrade-stage/<name>/`, `scp` them to
`<deploy_root>/user_data/config.json` + `<deploy_root>/docker-compose.yml`,
then run `docker compose …` over SSH. The compose port mapping uses
`0.0.0.0:<port>:<port>` so the agent can reach the API. Requires
`host=ssh` + `ssh_target` on the instance. Failures report the failing step
(`mkdir` / `scp config.json` / `scp docker-compose.yml` / `compose`).

## Sync — `ft_sync_user_data`

`rsync -az` between local `user_data` and the remote instance's `user_data`.
`direction` push (local→remote, default) or pull. `delete: true` adds
`--delete`. `source_path`/`dest_path` override the default (the instance
`user_data`). Remote instances must be `host=ssh` with `ssh_target` and
`user_data` set. Dry-run returns the exact rsync command.

## Bootstrap a remote host — `ft_bootstrap_host`

Provisions a fresh remote host for freqtrade:

- `method: docker` (default) — installs Docker Engine via `get.docker.com`
  (if missing), then `mkdir -p <root>/user_data/logs <root>/user_data/strategies`.
- `method: bare` — requires `python3` + `git`; clones freqtrade `develop`,
  creates a `.venv`, `pip install -e .`, then the same user_data tree.
  (Install TA-Lib separately if you plan to backtest.)

`install_root` defaults to the parent of the instance `user_data`. The script
is piped to `ssh … bash -s`.

## Order of operations for a new remote instance

1. `ft_instances_add` (host=ssh, ssh_target, user_data, strategy, exchange).
2. `ft_secret_set … ssh_key` (and passphrase if the key is encrypted).
3. `ft_bootstrap_host` (docker) → dry-run, then run.
4. `ft_deploy_ssh` → dry-run, then `up`.
5. `ft_ping` to confirm the API is reachable (remote API must bind
   `0.0.0.0` — see `references/topology.md`).