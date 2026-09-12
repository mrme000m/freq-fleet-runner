# dsh-ffm — the standalone Freqtrade Fleet Manager agent container.
#
# Serves the DeepSeek Harness Web GUI (`dsh web`) on :3081 with the
# freqtrade-fleet-manager plugin loaded into the `web` profile, defaulting
# every session to the `ffm` agent preset — a CLONE of the shipped "Creator
# Mode" (`cordis`) preset (the self-modifying preset used to author other
# presets/plugins) with a persona and skill for operating the Freqtrade
# fleet. Public path: https://dsh-ffm.00m.indevs.in (Cloudflare tunnel).
#
# Modeled on the grid-ga container (tvcli repo, docker/ga/): the same boot
# contract (vault_loader → dsh-home seed → settings render → foreground dsh
# web) and the same ponytail patch so `dsh web --host 0.0.0.0` is allowed.
# dsh version: npm `latest` 0.1.5-rc.1 (verified in this workspace against
# master HEAD c291e7961a51 — the freqtrade-fleet-manager plugin installs
# cleanly on it via the local-checkout path, all 54 tools register, the web
# profile serves, and every preset row of the cloned cordis composition
# resolves). Deliberately slim: NO Go, NO browser/Xvfb, NO PocketBase — the
# agent manages the fleet over each instance's REST API.
#
# The freqtrade-fleet-manager plugin ships from THIS repo (built lib/ is
# committed; `pnpm run build` in CI refreshes it before the docker build).
# It is installed into the `web` profile with the documented local-checkout
# path (`dsh plugin --profile web add /opt/ffm-plugin`).
#
# Secrets are NEVER baked: the settings template carries an
# @CF_ACCOUNT_ID@ placeholder rendered at boot from runtime env
# (vault_loader → /data/secrets/grid-vault.env).

FROM debian:bookworm-slim AS runtime

ENV DEBIAN_FRONTEND=noninteractive

# ── base OS packages: runtime deps + the fleet-manager toolchain ──────────
# curl (REST transport), git/jq (setup), python3 (vault_loader + preset
# helpers), openssh-client + rsync (the ft_deploy_ssh / ft_sync_user_data
# deploy surface — ssh/scp/rsync on PATH). Layer-order mirrors the grid/ga
# images: OS-level layers first — they change least often.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl unzip bash procps tzdata \
      git jq \
      openssh-client rsync \
      python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# ── Node 22 (NodeSource) — dsh and the plugin forwarder need >= 22 ─────────
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version

# ── Bitwarden CLI (vault_loader.sh machine-auth at container boot) ────────
RUN npm install -g --no-fund --no-audit @bitwarden/cli \
    && bw --version

# ── DSH (deepseek-harness) — the agent runtime ─────────────────────────────
# dsh = npm `latest` 0.1.5-rc.1 (the freqtrade-fleet-manager plugin's
# peer-compat table lists 0.1.0-rc.7 / 0.1.1-rc.2, but 0.1.5-rc.1 was verified
# compatible in this workspace — `dsh plugin --profile web add` installs it,
# `apply()` registers the 54 tools, and all 26 preset rows of the cloned
# cordis composition resolve). pnpm >= 10 (the `dsh plugin` forwarder) + the
# ponytail patch so a container bind on all interfaces is allowed (reachable
# only via the docker-network tunnel path + the 127.0.0.1 host publish). The
# npm cache is dropped IN THE SAME LAYER so the bytes never enter the image
# history. Pin any future dsh with a verified-compatible version by passing
# --build-arg DSH_VERSION=<ver>.
ARG DSH_VERSION=0.1.5-rc.1
COPY docker/dsh-ffm/dsh_ponytail_patch.py /tmp/dsh_ponytail_patch.py
RUN set -eux; \
    npm install -g --no-fund --no-audit pnpm@10; \
    npm install -g --no-fund --no-audit @deepseek-ai/dsh@${DSH_VERSION}; \
    rm -rf /root/.npm/_cacache; \
    python3 /tmp/dsh_ponytail_patch.py \
        "$(npm root -g)/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-web-app/lib/startup.js"; \
    dsh --version

# ── freqtrade-fleet-manager plugin into the `web` profile ─────────────────
# The built lib/ is committed in this repo (CI refreshes it with
# `pnpm run build` before the docker build — the "ships the current lib/"
# contract from the plugin README). The local-checkout install makes the
# host-plane bundle row (`cordis.patch.yml`) part of the profile bundle
# stack, so the 54 `ft_*` tools + the `/freqtrade` API + the Settings
# dashboard register for every session. If `dsh plugin add` is blocked on
# a pnpm build-script allowlist, the pnpm_allowbuilds remedy parses the
# demanded keys from the failure log, merges them into the profile's
# pnpm-workspace.yaml and retries once (same remedy as grid-ga).
COPY freqtrade-fleet-manager/package.json freqtrade-fleet-manager/cordis.patch.yml \
     freqtrade-fleet-manager/README.md freqtrade-fleet-manager/LICENSE /opt/ffm-plugin/
COPY freqtrade-fleet-manager/lib/ /opt/ffm-plugin/lib/
COPY docker/dsh-ffm/pnpm_allowbuilds.py /tmp/pnpm_allowbuilds.py
RUN set -eux; \
    if ! DSH_HOME=/opt/dsh-home dsh plugin --profile web add /opt/ffm-plugin \
            > /tmp/plugin-add.log 2>&1; then \
        cat /tmp/plugin-add.log; \
        python3 /tmp/pnpm_allowbuilds.py /opt/dsh-home/profiles/web /tmp/plugin-add.log; \
        DSH_HOME=/opt/dsh-home dsh plugin --profile web add /opt/ffm-plugin; \
    fi; \
    test -f /opt/dsh-home/profiles/web/node_modules/freqtrade-fleet-manager/lib/index.js

# ── FFM payloads (cheap COPYs — always LAST) ───────────────────────────────
# All preset payloads land in the SEED home /opt/dsh-home; the entrypoint
# seeds the writable runtime home (DSH_HOME=/data/dsh, the ffm-dsh volume)
# with revision-refresh semantics (first-boot copy, per-file refresh
# preserving operator edits). The settings.yaml is a TEMPLATE (the
# @CF_ACCOUNT_ID@ placeholder renders at boot).
ARG FFM_PRESET_REV=dev
COPY docker/dsh-ffm/agent-presets/ffm/ /opt/dsh-home/.agent-presets/ffm/
COPY docker/dsh-ffm/dsh-settings.yaml /opt/dsh-home/settings.yaml
COPY docker/dsh-ffm/entrypoint.sh /app/ffm/entrypoint.sh
COPY docker/dsh-ffm/healthcheck.sh /app/ffm/healthcheck.sh
COPY docker/dsh-ffm/vault_loader.sh /app/ffm/vault_loader.sh
RUN chmod +x /app/ffm/entrypoint.sh /app/ffm/healthcheck.sh /app/ffm/vault_loader.sh \
    && mkdir -p /data/dsh /data/secrets /data/bw-cli \
    && printf '%s\n' "${FFM_PRESET_REV}" > /opt/dsh-home/.baked-rev

ENV DSH_HOME=/data/dsh \
    DSH_WEB_PORT=3081 \
    GRID_BIND_HOST=0.0.0.0

# dsh web only (in-container :3081; the host publish is 127.0.0.1:3083 —
# the public path is the CF tunnel at dsh-ffm.00m.indevs.in).
EXPOSE 3081
STOPSIGNAL SIGTERM
HEALTHCHECK --start-period=2m --interval=30s --timeout=10s --retries=3 \
  CMD curl -fs -m 5 http://127.0.0.1:3081/ || exit 1
ENTRYPOINT ["/app/ffm/entrypoint.sh"]