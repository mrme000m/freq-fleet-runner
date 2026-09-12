#!/usr/bin/env bash
# ffm-run.sh — (re)start the dsh-ffm container (the standalone Freqtrade
# Fleet Manager agent) on the deployment host. Invoked over SSH by
# .github/workflows/dsh-ffm-deploy.yml after the image has reached the host
# (registry pull or docker load); also runnable by hand on the host.
# Idempotent: named volumes are created on first use (ffm-dsh holds the
# seeded dsh home + the fleet registry; ffm-secrets the vault-resolved env;
# ffm-bwcli the bw CLI login state — all survive redeploys), the ffm-net
# network is created when missing, a running container is stopped GRACEFULLY
# first (SIGTERM + 60s), then replaced.
#
# Env:
#   IMAGE     image to run                    (default dsh-ffm:local)
#   NAME      container name                  (default dsh-ffm)
#   ENV_FILE  BW_* + CF_TUNNEL_TOKEN env file (default /opt/dsh-ffm/.env)
#   DSH_FFM_DOCKER_SOCK 1   mount /var/run/docker.sock into the container so
#                           `host: docker` deploy flows work (default off —
#                           the socket grants host-wide docker control)
#
# Ports are published on 127.0.0.1 ONLY (127.0.0.1:3083 → container :3081);
# the public path is the CF tunnel — dsh-ffm.00m.indevs.in → http://dsh-ffm:3081
# on the ffm-net docker network (ingress reconciled by the deploy workflow's
# Cloudflare-API step; the `dsh-ffm-cloudflared` connector container below
# joins ffm-net and runs the tunnel with CF_TUNNEL_TOKEN). Reach the web UI
# directly through an SSH tunnel: ssh -L 3083:localhost:3083 <host>
set -euo pipefail

IMAGE="${IMAGE:-dsh-ffm:local}"
NAME="${NAME:-dsh-ffm}"
ENV_FILE="${ENV_FILE:-/opt/dsh-ffm/.env}"

[ -f "$ENV_FILE" ] || { echo "ffm-run: missing env file $ENV_FILE" >&2; exit 1; }
for key in BW_URL BW_CLIENTID BW_CLIENTSECRET BW_PASSWORD; do
  val="$(grep -E "^${key}=" "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
  [ -n "$val" ] || { echo "ffm-run: $ENV_FILE is missing a value for $key (BW_* are required for vault_loader)" >&2; exit 1; }
done
CF_TUNNEL_TOKEN="$(grep -E '^CF_TUNNEL_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2-)"

# docker needs sudo when the ssh user is not in the docker group
DOCKER="${DOCKER_CMD:-}"
if [ -z "$DOCKER" ]; then
  if docker info >/dev/null 2>&1; then DOCKER=docker; else DOCKER="sudo docker"; fi
fi

# ── volumes ─────────────────────────────────────────────────────────────────
for v in ffm-dsh ffm-secrets ffm-bwcli; do
  $DOCKER volume create "$v" >/dev/null
done

# ── network ────────────────────────────────────────────────────────────────
# ffm-net carries the public traffic: the dsh-ffm-cloudflared connector
# joins it and reaches dsh-ffm's dsh web by docker DNS (http://dsh-ffm:3081).
$DOCKER network create ffm-net >/dev/null 2>&1 || true

# ── graceful replace: SIGTERM + up to 60s settle, then force-remove ────────
if $DOCKER inspect "$NAME" >/dev/null 2>&1; then
  echo "ffm-run: stopping old $NAME (graceful, up to 60s)…"
  $DOCKER stop -t 60 "$NAME" >/dev/null 2>&1 || true
  $DOCKER rm -f "$NAME" >/dev/null 2>&1 || true
fi

echo "ffm-run: starting $NAME from $IMAGE…"
RUN_ARGS=(run -d --name "$NAME" \
  --restart unless-stopped \
  --stop-timeout 60 \
  --network ffm-net \
  --env-file "$ENV_FILE" \
  -p 127.0.0.1:3083:3081 \
  -v ffm-dsh:/data/dsh \
  -v ffm-secrets:/data/secrets \
  -v ffm-bwcli:/data/bw-cli)
if [ "${DSH_FFM_DOCKER_SOCK:-0}" = "1" ]; then
  RUN_ARGS+=(-v /var/run/docker.sock:/var/run/docker.sock)
fi
$DOCKER "${RUN_ARGS[@]}" "$IMAGE"

# ── connector sidecar ──────────────────────────────────────────────────────
# When CF_TUNNEL_TOKEN is present, keep the cloudflared connector ensured on
# ffm-net so dsh-ffm.00m.indevs.in resolves without host port publishing.
# Replaced on every redeploy (token from the env file — never logged).
if [ -n "$CF_TUNNEL_TOKEN" ]; then
  $DOCKER rm -f dsh-ffm-cloudflared >/dev/null 2>&1 || true
  $DOCKER run -d --name dsh-ffm-cloudflared \
    --restart unless-stopped \
    --network ffm-net \
    cloudflare/cloudflared:latest tunnel --no-autoupdate run \
    --token "$CF_TUNNEL_TOKEN"
  echo "ffm-run: cloudflared connector up (dsh-ffm.00m.indevs.in → http://dsh-ffm:3081 via ffm-net)"
else
  echo "ffm-run: no CF_TUNNEL_TOKEN — connector not started (dsh-ffm web reachable via ssh -L 3083:localhost:3083)"
fi

echo "ffm-run: container up — boot (vault load → dsh home seed → settings render) takes ~30-90s"

# image hygiene: drop dangling layers + older SHA-tagged versions of this
# image left by previous redeploys (the running image and the moving `main` /
# `buildcache` / `local` tags are kept; only the dsh-ffm repo pattern matches).
$DOCKER image prune -f >/dev/null || true
CUR_IMG="$($DOCKER inspect --format '{{.Image}}' "$NAME" 2>/dev/null || true)"
for img in $($DOCKER images --format '{{.Repository}}:{{.Tag}}' \
              | awk -F: '$1 ~ /dsh-ffm$/ && $2 !~ /^(main|buildcache|local|<none>)$/'); do
  full="$($DOCKER inspect --format '{{.Id}}' "$img" 2>/dev/null || true)"
  [ -n "$full" ] && [ "$full" != "$CUR_IMG" ] && $DOCKER rmi "$img" >/dev/null 2>&1 || true
done