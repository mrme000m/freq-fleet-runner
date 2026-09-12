# dsh-ffm — Freqtrade Fleet Manager agent container

The standalone **Freqtrade Fleet Manager** agent. A slim Debian container that
serves the DeepSeek Harness Web GUI (`dsh web`) on `:3081` with the
`freqtrade-fleet-manager` plugin loaded into the `web` profile, defaulting
every session to the **`ffm`** agent preset — a clone of the shipped **Creator
Mode (`cordis`)** preset (the self-modifying preset used to author other
presets/plugins) plus a persona and the `ffm-operations` skill for operating
the fleet.

Public path: **https://dsh-ffm.00m.indevs.in** (Cloudflare tunnel; DNS + tunnel
ingress reconciled by the deploy workflow's Cloudflare-API step). Reach the web
UI directly through an SSH tunnel: `ssh -L 3083:localhost:3083 <host>` then
http://localhost:3083.

## Contents

| Path | Purpose |
|------|---------|
| `Dockerfile` | The image (multi-ARCH amd64; dsh `0.1.5-rc.1` default) |
| `entrypoint.sh` | PID-1 supervisor: vault → seed → settings render → `exec dsh web` |
| `healthcheck.sh` | `curl http://127.0.0.1:$DSH_WEB_PORT/` (Docker HEALTHCHECK) |
| `vault_loader.sh` | Bitwarden machine-auth loader, `BW_VAULT_ONLY=cf,llm` |
| `dsh-settings.yaml` | LLM catalog **template** (CF Workers AI default; `@CF_ACCOUNT_ID@` rendered at boot) |
| `dsh_ponytail_patch.py` | Patches dsh-web-app so `--host 0.0.0.0` is allowed |
| `pnpm_allowbuilds.py` | pnpm build-script allowlist remedy for `dsh plugin add` |
| `env.example` | Host env-file shape (`BW_*` + `CF_TUNNEL_TOKEN`) |
| `ffm-run.sh` | Host runner: named volumes/network, graceful replace, optional cloudflared sidecar |
| `agent-presets/ffm/` | The `ffm` preset (clone of cordis + ffm-operations skill) |
| `README.md` | This file |

## Versions & provenance (verified)

- **dsh**: npm `latest` **`0.1.5-rc.1`** — verified compatible with the
  `freqtrade-fleet-manager` plugin in this workspace: `dsh plugin --profile web
  add` installs it cleanly, `apply()` registers all 54 tools, the web profile
  serves, and every one of the 26 preset rows in the cloned cordis composition
  resolves against the profile modules. (The plugin's peerDeps table lists
  `0.1.0-rc.7 || 0.1.1-rc.2`, but `0.1.5-rc.1` works — verified live.)
- **cordis preset source**: `deepseek-harness` master HEAD `c291e7961a51`
  (`packages/preset/agent-presets/presets/cordis/`). The shipped
  `0.1.5-rc.1` preset hash matches master (`2b8d89c3…`), and the `ffm` clone's
  35 composition rows are byte-identical to the shipped cordis — only the
  persona paragraph and `preset.yml` metadata differ.
- **ponytail patch**: the `0.0.0.0` guard string + file path
  (`@deepseek-ai/dsh-web-app/lib/startup.js`) are identical in `0.1.5-rc.1`,
  so the same patch applies. `--host 0.0.0.0` is required so the tunnel
  connector can reach dsh web over docker DNS.

## Build

```sh
# from the repo root (context is the whole repo, but .dockerignore keeps the
# freqtrade/ clone and node_modules out)
docker build -f docker/dsh-ffm/Dockerfile -t dsh-ffm:local .
```

The build installs the plugin with the documented local-checkout path
(`dsh plugin --profile web add /opt/ffm-plugin`); the built `lib/` is committed
in `freqtrade-fleet-manager/` and CI refreshes it with `pnpm run build` before
the docker build. Build args:

| Arg | Default | Purpose |
|-----|---------|---------|
| `DSH_VERSION` | `0.1.5-rc.1` | dsh version (pin a verified-compatible one if the plugin table changes) |
| `FFM_PRESET_REV` | `dev` | stamps `/opt/dsh-home/.baked-rev` (entrypoint uses it to refresh the seeded preset across image revisions) |

## Run (host, via ffm-run.sh)

```sh
# 1. write /opt/dsh-ffm/.env  (chmod 600) — see env.example:
#    BW_URL / BW_USERNAME / BW_PASSWORD / BW_CLIENTID / BW_CLIENTSECRET
#    CF_TUNNEL_TOKEN (optional; enables the cloudflared connector sidecar)
# 2. ship the image + ffm-run.sh (the dsh-ffm-deploy workflow does this)
IMAGE=dsh-ffm:local bash /opt/dsh-ffm/ffm-run.sh
```

`ffm-run.sh` is idempotent: named volumes `ffm-dsh` (the persistent DSH home —
fleet registry, web profile), `ffm-secrets`, `ffm-bwcli`; docker network
`ffm-net`; graceful replace (SIGTERM + 60s); ports on `127.0.0.1:3083:3081`
only. When `CF_TUNNEL_TOKEN` is set it also ensures the `dsh-ffm-cloudflared`
connector on `ffm-net`, so `dsh-ffm.00m.indevs.in` resolves
(`→ http://dsh-ffm:3081` by docker DNS). Boot is ~30-90s (vault → seed →
settings render → dsh web).

## Deploy

`.github/workflows/dsh-ffm-deploy.yml` (repo root) — on push touching
`docker/dsh-ffm/**`, `freqtrade-fleet-manager/**` or the workflow itself:

1. `pnpm run build` in `freqtrade-fleet-manager` (ships the current `lib/`)
2. Build + push to `ghcr.io/<repo>/dsh-ffm` (`:sha`, `:main`, layer cache
   `:buildcache`; `FFM_PRESET_REV=$sha`)
3. Transport: `ghcr` (host pulls, layer-diffed) or `ssh-stream` fallback
   (docker save | ssh | docker load); direct SSH or Cloudflare-Access proxied
   (secrets `SSH_*`, `CF_ACCESS_CLIENT_ID/SECRET` when `SSH_PROXY` set)
4. Write `/opt/dsh-ffm/.env` (chmod 600) from repo secrets, ship `ffm-run.sh`,
   run it
5. Cloudflare-API step (non-fatal): reconcile tunnel ingress
   `dsh-ffm.00m.indevs.in → http://dsh-ffm:3081` + DNS CNAME
6. Health gate: `curl http://127.0.0.1:3083/` up to 6 min

## Secrets contract

- The container loads **only** Cloudflare + LLM provider items + the GitHub
  fleet token (`BW_VAULT_ONLY=cf,llm,gh`): `opencode-cloudflare`
  (`CLOUDFLARE_ACCOUNT_ID`/`CLOUDFLARE_API_KEY`), `cloudflare-tunnels`
  (folder `cloudflare`: `CF_ACCOUNT_ID` + `CF_API_TOKEN_READ/WRITE`),
  `provider-keys` (`NVIDIA_API_KEY`/`OPENROUTER_API_KEY`/`MISTRAL_API_KEY`),
  and `github-fleet-token` (notes `GH_FLEET_TOKEN=ghp_…` — a fine-grained PAT
  scoped to `mrme000m/freq-fleet-runner`, `Contents: read & write`).
- No trading credentials are baked or loaded. Per-instance API secrets live in
  the container's `credentials` host service via `ft_secret_set` (refs
  `FTMGR_<NAME>_<KIND>`), persisted in the `ffm-dsh` volume.
- Never bake `@CF_ACCOUNT_ID@`: it renders at boot from runtime env
  (fallback chain `CF_ACCOUNT_ID` → `CLOUDFLARE_ACCOUNT_ID`).

## Operational notes

- **Image updates vs operator edits**: the entrypoint refreshes the seeded
  `ffm` preset files across image revisions **per-file** — a preset file the
  operator edited locally (diverges from `.last-seed/`) is preserved. A
  hand-edited `settings.yaml` is likewise preserved (sha256 guard); otherwise
  the image template wins.
- **Fleet registry** lives at `/data/dsh/.freqtrade-instances.json` (the
  `ffm-dsh` volume), reachable via the `ft_*` tools + `/freqtrade` API +
  Settings → Freqtrade Fleet dashboard.
- **Logs**: `dsh-web.log` in the volume + container stdout
  (`docker logs`), boot progress under `[ffm-entrypoint ...]`.

## Self-modification — the agent updates its own source

The `ffm` preset is a clone of Creator Mode (`cordis`), so the agent is meant
to *author and improve its own code*. To make those edits durable (and to let
them flow back to the dev workspace), the container keeps a git checkout of
this repo in the persistent volume:

- **Checkout**: `/data/dsh/repo` — a clone of
  `https://github.com/mrme000m/freq-fleet-runner.git`, fetched to `origin/main`
  at every boot (first boot clones; later boots `git fetch`). It lives in the
  `ffm-dsh` volume, so in-progress edits survive redeploys.
- **Push credential**: `GH_FLEET_TOKEN` (vault item `github-fleet-token`),
  supplied to git via `core.askPass` → `git-askpass.sh` (the token is never
  written into git config or a file — read from env at push time, so rotation
  is transparent). Identity is `dsh-ffm-agent <dsh-ffm-agent@mrme000m.invalid>`.
- **Round-trip**: the agent edits `docker/dsh-ffm/**` or
  `freqtrade-fleet-manager/**` in `/data/dsh/repo`, `git commit` + `git push
  origin main`. The push triggers `.github/workflows/dsh-ffm-deploy.yml`
  (path-filtered on those directories), which rebuilds + redeploys this
  container — the agent has updated itself. The dev workspace
  (`grid/0/freqtrade`) is a git checkout of the same repo and `git pull`s the
  agent's changes back.
- **Gating**: the clone/fetch fail soft (warn + continue) when GitHub is
  unreachable; push is the only step that requires the token. Without
  `GH_FLEET_TOKEN` the agent can still edit locally, just not push.