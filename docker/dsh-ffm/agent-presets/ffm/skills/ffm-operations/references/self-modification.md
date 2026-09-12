# self-modification.md — updating this agent's own code

This container keeps its own source at **`/data/dsh/repo`** — a git clone of
`mrme000m/freq-fleet-runner`:

- `docker/dsh-ffm/**` — the container build (Dockerfile, entrypoint,
  vault_loader, agent preset + skills).
- `freqtrade-fleet-manager/**` — the plugin (`src/index.js` host,
  `client/index.js` browser, `cordis.patch.yml`, `tsdown.config.mjs`).
- `.github/workflows/**` — the deploy workflows.

Make changes durable through the full round-trip:

1. **Edit** files under `/data/dsh/repo` (`docker/dsh-ffm/…` or
   `freqtrade-fleet-manager/…`). The mounted preset/skill files under
   `/data/dsh/.agent-presets/ffm/…` are copies — keep them in sync too when you
   want the change live before the redeploy.
2. **Rebuild** after touching `freqtrade-fleet-manager/src/index.js`:
   `cd /data/dsh/repo/freqtrade-fleet-manager && pnpm install
   --frozen-lockfile && pnpm run build`. Commit the refreshed `lib/` (CI
   rebuilds too, but committing `lib/` means the image ships your build).
   After touching `client/index.js`, the build also refreshes `lib/client.js`.
3. **Verify** nothing stray changed: `git -C /data/dsh/repo status`.
4. **Commit + push**:
   `git -C /data/dsh/repo add -A && git -C /data/dsh/repo commit -m "…" &&
   git -C /data/dsh/repo push origin main`.
   The push triggers the `dsh-ffm-deploy` workflow, which rebuilds + redeploys
   this container (the agent self-updates). Push auth comes from
   `GH_FLEET_TOKEN` via the git askpass helper — never echo or log it.
5. **Scope** — push only to `main`, and keep changes under `docker/dsh-ffm/**`
   and `freqtrade-fleet-manager/**` so the path-filtered workflow fires.

## Which file for which change

| Change | File |
|---|---|
| New/edited `ft_*` tool, schema, /freqtrade API | `freqtrade-fleet-manager/src/index.js` |
| Dashboard UI | `freqtrade-fleet-manager/client/index.js` |
| Tool/behavior documentation the agent reads | `docker/dsh-ffm/agent-presets/ffm/skills/ffm-operations/**` |
| Agent persona / preset wiring | `docker/dsh-ffm/agent-presets/ffm/agent.cordis.yml` |
| Container boot / secrets / git push auth | `docker/dsh-ffm/{entrypoint,vault_loader,git-askpass}.sh` |

## Post-push notes

- The redeploy replaces the running container; the session workspace and the
  registry (`/data/dsh/.freqtrade-instances.json`) live on the persistent
  volume, so fleet state survives.
- Tool count appears in the plugin's own `apply()` log line and in the
  persona/README — update all three when you add or remove a tool.