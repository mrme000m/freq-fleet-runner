#!/usr/bin/env bash
# ffm-entrypoint — single-purpose supervisor for the dsh-ffm container.
#
# The Freqtrade Fleet Manager agent (dsh web serving the `ffm` preset — a
# clone of the Creator Mode `cordis` preset — with the freqtrade-fleet-manager
# bundle mounted in the `web` profile) runs as PID 1 in the FOREGROUND: its
# death IS the container's death, and the `--restart unless-stopped` policy
# brings it back. There is deliberately nothing else to supervise — no
# browser, no trading daemon. The fleet is reached over each instance's REST
# API (the registry lives in the ffm-dsh volume).
#
# Boot order:
#   (1) vault_loader.sh with BW_VAULT_ONLY=cf,llm — materialize the
#       Cloudflare vault items (opencode-cloudflare + cloudflare-tunnels)
#       AND the LLM provider keys (provider-keys) into
#       /data/secrets/grid-vault.env; fail-soft when the BW_* env is unset.
#   (2) source the vault env when produced + bridge the CF env names the
#       dsh/prime-agent Cloudflare stack reads.
#   (3) seed the runtime DSH home (/data/dsh — the ffm-dsh volume) from the
#       baked seed home /opt/dsh-home, with revision-refresh logic
#       (first-boot copy, .baked-rev/.seeded-rev compare, per-file preset
#       refresh preserving operator edits, settings.yaml render guarded by
#       sha256 hand-edit detection).
#   (4) exec dsh web in the foreground (cwd = DSH_HOME so the fleet
#       manager's sandbox workspace root resolves to the persistent volume
#       and the registry lands at /data/dsh/.freqtrade-instances.json).
set -uo pipefail

FFM=/app/ffm
export DSH_HOME="${DSH_HOME:-/data/dsh}"
export DSH_WEB_PORT="${DSH_WEB_PORT:-3081}"
SEED_HOME=/opt/dsh-home

log()  { echo "[ffm-entrypoint $(date -u +%H:%M:%S)] $*"; }
warn() { echo "[ffm-entrypoint $(date -u +%H:%M:%S)] WARN: $*" >&2; }

log "dsh-ffm container starting (dsh web :$DSH_WEB_PORT, DSH_HOME=$DSH_HOME)"
log "  arch: $(uname -m)   node: $(node --version 2>/dev/null || echo n/a)   python: $(python3 --version 2>&1)"

mkdir -p "$DSH_HOME" /data/secrets /data/bw-cli

# ── (1) vault-driven secrets (Cloudflare + LLM provider items) ──────────────
# The fleet manager container loads NO trading secrets (no WT creds, no
# exchange API keys) — BW_VAULT_ONLY=cf,llm restricts vault_loader.sh to: the
# two Cloudflare items (opencode-cloudflare: CLOUDFLARE_ACCOUNT_ID/API_KEY;
# cloudflare-tunnels: CF_ACCOUNT_ID + CF_API_TOKEN_READ/WRITE for the cf
# skill) and the LLM provider keys (provider-keys → NVIDIA_API_KEY,
# OPENROUTER_API_KEY, MISTRAL_API_KEY). Fail-soft: no BW_* env → exit 0
# "vault disabled" inside the loader; a failed load warns and continues.
if [ -n "${BW_URL:-}" ] && [ -n "${BW_CLIENTID:-}" ] && [ -n "${BW_CLIENTSECRET:-}" ] && [ -n "${BW_PASSWORD:-}" ]; then
  log "vault enabled — running vault_loader.sh (BW_VAULT_ONLY=cf,llm)"
  if BW_VAULT_ONLY=cf,llm bash "$FFM/vault_loader.sh"; then
    log "vault load complete"
  else
    warn "vault_loader.sh failed (exit $?) — continuing with inline env"
  fi
else
  log "vault disabled (no BW_* env) — using inline env"
fi

# ── (2) source the vault env + bridge the CF env names ─────────────────────
if [ -f /data/secrets/grid-vault.env ]; then
  # shellcheck disable=SC1091
  . /data/secrets/grid-vault.env
  log "sourced /data/secrets/grid-vault.env"
  for rc in /root/.bashrc /root/.profile; do
    if ! grep -q 'grid-vault.env' "$rc" 2>/dev/null; then
      printf '\n# dsh-ffm: vault-resolved runtime secrets (CF tokens, …)\n[ -f /data/secrets/grid-vault.env ] && . /data/secrets/grid-vault.env\n' >> "$rc"
    fi
  done
else
  log "no /data/secrets/grid-vault.env (vault disabled or nothing resolved)"
fi
# env bridge — the exact names the dsh Cloudflare stack reads
# (token CLOUDFLARE_AI_TOKEN falling back to CLOUDFLARE_API_KEY; account
# CF_ACCOUNT_ID falling back to CLOUDFLARE_ACCOUNT_ID).
export CLOUDFLARE_AI_TOKEN="${CLOUDFLARE_AI_TOKEN:-${CLOUDFLARE_API_KEY:-}}"
export CF_ACCOUNT_ID="${CF_ACCOUNT_ID:-${CLOUDFLARE_ACCOUNT_ID:-}}"

# ── (3) seed the runtime DSH home from the baked seed home ─────────────────
# Same revision-refresh logic as the grid-ga entrypoint: the ffm-dsh volume
# carries the seeded home from the PREVIOUS image, so a boot with a changed
# image revision refreshes the baked preset files PER-FILE (a file the
# operator edited locally — no longer identical to the previously seeded copy
# under .last-seed/ — is preserved untouched), while the pnpm-managed web
# profile is replaced wholesale unless .keep-profile marks it. settings.yaml
# is DERIVED: rendered each boot from the baked template with the runtime CF
# account id (never baked); the render skips (warns) when a hand-edited
# settings.yaml is detected.
BAKED_REV="$(cat "$SEED_HOME/.baked-rev" 2>/dev/null || echo unknown)"
SEEDED_REV="$(cat "$DSH_HOME/.seeded-rev" 2>/dev/null || echo none)"
if [ "$BAKED_REV" != "$SEEDED_REV" ]; then
  if [ "$SEEDED_REV" = "none" ]; then
    log "dsh home: first boot — seeding $DSH_HOME from $SEED_HOME (rev $BAKED_REV)"
    cp -a "$SEED_HOME/." "$DSH_HOME/"
  else
    log "dsh home: image revision changed ($SEEDED_REV → $BAKED_REV) — refreshing baked files"
    for f in preset.yml agent.cordis.yml skills/ffm-operations/SKILL.md \
             skills/editing-cordis-compositions/SKILL.md \
             skills/cordis-plugin-development/SKILL.md; do
      if [ ! -e "$DSH_HOME/.agent-presets/ffm/$f" ] || cmp -s "$DSH_HOME/.last-seed/ffm/$f" "$DSH_HOME/.agent-presets/ffm/$f"; then
        mkdir -p "$DSH_HOME/.agent-presets/ffm/$(dirname "$f")"
        cp -a "$SEED_HOME/.agent-presets/ffm/$f" "$DSH_HOME/.agent-presets/ffm/$f"
      else
        warn "dsh home: .agent-presets/ffm/$f was edited locally — preserved (image update skipped for it)"
      fi
    done
    if [ -e "$DSH_HOME/.keep-profile" ]; then
      log "dsh home: .keep-profile present — baked web profile left untouched"
    else
      rm -rf "$DSH_HOME/profiles/web"
      mkdir -p "$DSH_HOME/profiles"
      cp -a "$SEED_HOME/profiles/web" "$DSH_HOME/profiles/web"
    fi
  fi
  echo "$BAKED_REV" > "$DSH_HOME/.seeded-rev"
  rm -rf "$DSH_HOME/.last-seed"
  mkdir -p "$DSH_HOME/.last-seed"
  cp -a "$SEED_HOME/.agent-presets/ffm" "$DSH_HOME/.last-seed/ffm"
fi

# settings.yaml — derived from the baked template each boot. Two sources of
# truth: (a) the baked TEMPLATE (image-owned) and (b) operator edits to the
# rendered file. When the template's sha changes (image updated the model
# catalog etc.), the template WINS and is re-rendered; when the template is
# unchanged but the rendered file diverges from the last render, it was
# hand-edited and is preserved.
if [ -n "$CF_ACCOUNT_ID" ]; then
  render=1
  TPL_SHA="$(sha256sum "$SEED_HOME/settings.yaml" | cut -d' ' -f1)"
  OLD_TPL_SHA="$(cat "$DSH_HOME/.settings-template.sha256" 2>/dev/null || true)"
  if [ -f "$DSH_HOME/settings.yaml" ]; then
    if [ "$TPL_SHA" != "$OLD_TPL_SHA" ]; then
      render=1   # image template changed — re-render (image is authoritative)
    else
      CUR_SHA="$(sha256sum "$DSH_HOME/settings.yaml" | cut -d' ' -f1)"
      REN_SHA="$(cat "$DSH_HOME/.settings.sha256" 2>/dev/null || true)"
      if [ -n "$REN_SHA" ] && [ "$CUR_SHA" != "$REN_SHA" ]; then
        render=0
        warn "dsh home: $DSH_HOME/settings.yaml was edited locally — preserved (not re-rendered)"
      fi
    fi
  fi
  if [ "$render" = "1" ]; then
    sed "s|@CF_ACCOUNT_ID@|$CF_ACCOUNT_ID|g" "$SEED_HOME/settings.yaml" > "$DSH_HOME/settings.yaml"
    chmod 600 "$DSH_HOME/settings.yaml"
    sha256sum "$DSH_HOME/settings.yaml" | cut -d' ' -f1 > "$DSH_HOME/.settings.sha256"
    printf '%s\n' "$TPL_SHA" > "$DSH_HOME/.settings-template.sha256"
    log "dsh home: settings.yaml rendered for the runtime CF account (default preset: ffm)"
  fi
else
  warn "dsh home: no CF account id in env — settings.yaml not rendered (sessions need the CLOUDFLARE_* env)"
fi

# ── (4) dsh web — the fleet manager agent, FOREGROUND (its death = container death) ──
# --trusted-host: the /api browser-trust fence accepts the bind host by
# default — requests arriving through the CF tunnel carry
# Host: dsh-ffm.00m.indevs.in and would be rejected without it. Logs go to
# BOTH /data/dsh/dsh-web.log (the persistent ffm-dsh volume) and stdout.
# cwd = DSH_HOME so the sandbox workspace root resolves to the volume: the
# freqtrade fleet registry lands at /data/dsh/.freqtrade-instances.json.
cd "$DSH_HOME"
log "starting dsh web on 0.0.0.0:$DSH_WEB_PORT (trusted host: ${DSH_TRUSTED_HOST:-dsh-ffm.00m.indevs.in})"
exec > >(tee -a "$DSH_HOME/dsh-web.log") 2>&1
exec dsh web \
  --host "${GRID_BIND_HOST:-0.0.0.0}" \
  --port "$DSH_WEB_PORT" \
  --no-open \
  --trusted-host "${DSH_TRUSTED_HOST:-dsh-ffm.00m.indevs.in}"