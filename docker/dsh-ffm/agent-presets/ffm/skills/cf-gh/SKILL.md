---
name: cf-gh
description: >
  Manage the Cloudflare-tunnel ↔ GitHub-runner infrastructure that fronts this
  dsh-ffm deployment, and access the Bitwarden dev vault — from inside the
  container. Covers: the CF read/write API tokens (CF_API_TOKEN_READ/WRITE),
  the VM-local cloudflared tunnel ingress (create/modify/remove host rules in
  /root/.cloudflared/config.yml + restart), the DNS CNAME records in the
  00m.indevs.in zone, SSH into the ephemeral fleet-runner VM, and the bw CLI
  for the dev Vaultwarden (read/write items, the notes KEY=VAL convention).
  Load before adding/changing a public host, touching the tunnel config, or
  reading/writing the vault.
---

# cf-gh — Cloudflare tunnel + GitHub runner + Bitwarden vault

This deployment is published to the public internet through a Cloudflare tunnel
that runs on an ephemeral GitHub Actions runner VM. This skill is the operator
playbook for that infrastructure and for the Bitwarden dev vault that holds its
credentials. Everything here runs **from inside this container** — the VM, the
tunnel, the DNS, and the vault are all reachable from your shell.

## 0. Infrastructure map (read this first)

- **The fleet-runner VM** — an ephemeral Ubuntu GitHub Actions runner brought
  up on demand by `.github/workflows/fleet-runner.yml` (manual dispatch, a
  keep-alive timer, then teardown). It runs:
  - an `openssh-server` (`runner` user, passwordless `sudo`, key-only login)
  - a **VM-local `cloudflared`** (a host process, NOT in docker) — the tunnel
    connector, kept alive by `/usr/local/bin/cloudflared-loop` (auto-restart)
  - the `dsh-ffm` container (this process's host) on a docker user-bridge
    network `ffm-net`
- **The tunnel** — `58052fa6-f501-4ba6-aabe-9167ea3b8086` (env `TUNNEL_ID`),
  account `CF_ACCOUNT_ID`. It is **locally configured**: the ingress table
  lives in `/root/.cloudflared/config.yml` ON THE VM (NOT the CF API — the
  `cfd_tunnel` remote-config endpoint does not manage a local-config tunnel).
  The default ingress (check before editing):
  ```yaml
  ingress:
    - hostname: freq-fleet.00m.indevs.in        # → ssh://localhost:22
    - hostname: freq-fleet-api.00m.indevs.in    # → http://localhost:8080
    - hostname: dsh-ffm.00m.indevs.in            # → http://127.0.0.1:3083  (this web UI)
    - service: http_status:404                   # catch-all MUST stay last
  ```
- **The DNS zone** — `00m.indevs.in` (Cloudflare). Each public host is a
  proxied CNAME `<host>.00m.indevs.in` → `<TUNNEL_ID>.cfargotunnel.com`.
- **`ssh ffm-vm`** — the entrypoint writes a `~/.ssh/config` Host entry for the
  VM (runner@<docker-bridge-gateway>, key `/data/ssh/id_ed25519`, no host-key
  checking). `sudo` works → you can edit `/root/.cloudflared`. If the VM is
  down (fleet-runner expired), `ssh ffm-vm true` fails — re-spin it with
  `gh workflow run fleet-runner.yml` (needs the gh token / self-mod path, see
  the `ffm-operations` self-modification reference).

## 1. Credentials available (all in your shell env)

| Env var | Source (vault item) | Scope | Use for |
|---|---|---|---|
| `CF_API_TOKEN_READ` | `cloudflare-tunnels` (folder `cloudflare`, field `read-all`) | read-all | GET tunnels, zones, DNS records, account info |
| `CF_API_TOKEN_WRITE` | `cloudflare-tunnels` (field `write-all`) | write-all | POST/PUT/DELETE DNS records (and anything write-all permits) |
| `CLOUDFLARE_API_KEY` | `opencode-cloudflare` | zone `00m.indevs.in` | secondary zone-scoped Bearer token (works for zone/DNS reads) |
| `CF_ACCOUNT_ID` | `cloudflare-tunnels` (field `account-id`) | — | the account id path segment |
| `TUNNEL_ID` | baked (`58052fa6-…`) | — | the fleet-runner tunnel id |
| `BW_URL` `BW_USERNAME` `BW_PASSWORD` `BW_CLIENTID` `BW_CLIENTSECRET` | deploy env file | dev vault (keys.00m.indevs.in) | bw CLI machine-auth + unlock |

Rules:
- **Use `CF_API_TOKEN_READ` for every query**; reach for `CF_API_TOKEN_WRITE`
  only for a confirmed change, and only the minimum call. Never `echo` a token
  value; `curl -H "Authorization: Bearer $CF_API_TOKEN_WRITE"` keeps it out of
  argv/logs, but `set -x` and `echo "$CF_API_TOKEN_WRITE"` leak it.
- The tunnel's ingress is **local-config** — the CF API can list the tunnel
  (`GET /accounts/$CF_ACCOUNT_ID/cfd_tunnel`) but CANNOT edit its ingress; you
  must edit `/root/.cloudflared/config.yml` over SSH and restart cloudflared.
- DNS, however, IS managed via the CF API (the write token) — the CNAMEs that
  point hosts at `<TUNNEL_ID>.cfargotunnel.com`.

## 2. SSH into the VM from the container

```bash
ssh ffm-vm 'hostname && uptime'                 # plain
ssh ffm-vm 'sudo cat /root/.cloudflared/config.yml'   # read the live ingress
ssh ffm-vm 'sudo docker ps --format "{{.Names}} {{.Status}}"'
```
- `ffm-vm` is configured by the entrypoint (runner@the docker bridge default
  route, key `/data/ssh/id_ed25519`). If `/data/ssh/id_ed25519` is missing the
  deploy did not provision the key — report it; do not try to SSH without it.
- The bridge gateway is usually `172.18.0.1`; if `ffm-vm` is unset, discover
  it: `gw=$(printf '%d.%d.%d.%d' $(awk 'NR>1&&$2=="00000000"{print $3;exit}' /proc/net/route | sed 's/../0x& /g' | awk '{print $4,$3,$2,$1}'))` (the route is little-endian hex).

## 3. Tunnel host management — add / modify / remove a public host

A public host needs **two** things: an ingress rule on the VM (the tunnel
routes it) AND a DNS CNAME (the public name resolves to the tunnel). Do both.

### 3a. Edit the VM ingress (idempotent, preserves other rules)

```bash
HOST=dsh-ffm.00m.indevs.in
SERVICE=http://127.0.0.1:3083        # the target on the VM (host port, not docker DNS)
ssh ffm-vm 'sudo python3 -' <<PY
import yaml, sys
host, service = "$HOST", "$SERVICE"
p = "/root/.cloudflared/config.yml"
cfg = yaml.safe_load(open(p))
ing = cfg.setdefault("ingress", [])
# update an existing rule for this host, else insert before the catch-all
for r in ing:
    if r.get("hostname") == host:
        r["service"] = service; break
else:
    idx = next((i for i,r in enumerate(ing) if not r.get("hostname")), len(ing))
    ing.insert(idx, {"hostname": host, "service": service})
open(p,"w").write(yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False))
print("ingress updated:", host, "->", service)
PY
```

### 3b. Restart cloudflared (DETACHED — or you kill your own SSH session)

cloudflared carries the SSH connection you're typing on; killing it in-band
drops you. The `cloudflared-loop` auto-restarts it, so just trigger a kill from
a `setsid`'d helper:
```bash
ssh ffm-vm "sudo setsid nohup sh -c 'sleep 2; pkill -f \"cloudflared tunnel\" || true' >/tmp/cf-restart.log 2>&1 &" || true
# the loop reconnects in ~5-10s; wait for it:
for i in $(seq 1 12); do ssh ffm-vm true 2>/dev/null && break || sleep 5; done
```

### 3c. The DNS CNAME (CF API, write token)

```bash
CF_API=https://api.cloudflare.com/client/v4
AUTH="Authorization: Bearer $CF_API_TOKEN_WRITE"
ZONE_ID=$(curl -fsS -m 20 -H "$AUTH" "$CF_API/zones?name=00m.indevs.in" | jq -r '.result[0].id')
TARGET="$TUNNEL_ID.cfargotunnel.com"
REC=$(curl -fsS -m 20 -H "$AUTH" "$CF_API/zones/$ZONE_ID/dns_records?name=$HOST" | jq -c '.result[0] // empty')
if [ -z "$REC" ]; then
  curl -fsS -m 20 -X POST -H "$AUTH" -H "Content-Type: application/json" \
    -d "{\"type\":\"CNAME\",\"name\":\"$HOST\",\"content\":\"$TARGET\",\"proxied\":true}" \
    "$CF_API/zones/$ZONE_ID/dns_records" | jq -e '.success'
elif [ "$(echo "$REC" | jq -r '.content')" = "$TARGET" ]; then
  echo "CNAME already correct"
else
  RID=$(echo "$REC" | jq -r '.id')
  curl -fsS -m 20 -X PUT -H "$AUTH" -H "Content-Type: application/json" \
    -d "{\"type\":\"CNAME\",\"name\":\"$HOST\",\"content\":\"$TARGET\",\"proxied\":true}" \
    "$CF_API/zones/$ZONE_ID/dns_records/$RID" | jq -e '.success'
fi
```

### 3d. Verify + remove

```bash
# health: any HTTP response (200 app / 401 token-gate / 404 no-matching-ingress) means DNS+tunnel up
curl -s -o /dev/null -w "%{http_code}\n" -m 10 "https://$HOST/"
# remove: delete the ingress rule, restart, delete the DNS record
ssh ffm-vm 'sudo python3 -' <<PY
import yaml; p="/root/.cloudflared/config.yml"; host="$HOST"
cfg=yaml.safe_load(open(p)); cfg["ingress"]=[r for r in cfg["ingress"] if r.get("hostname")!=host]
open(p,"w").write(yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False))
PY
# (3b restart), then delete the record:
RID=$(curl -fsS -m 20 -H "$AUTH" "$CF_API/zones/$ZONE_ID/dns_records?name=$HOST" | jq -r '.result[0].id // empty')
[ -n "$RID" ] && curl -fsS -m 20 -X DELETE -H "$AUTH" "$CF_API/zones/$ZONE_ID/dns_records/$RID" | jq -e '.success'
```

## 4. Bitwarden dev vault access (the bw CLI)

The dev vault is the self-hosted Vaultwarden at `$BW_URL` (keys.00m.indevs.in).
The bw CLI is installed (v2026.8.0); its state persists in `/data/bw-cli`. Auth
from your shell:

```bash
export BITWARDENCLI_APPDATA_DIR=/data/bw-cli
bw config server "$BW_URL" >/dev/null 2>&1 || true        # idempotent
bw login --apikey >/dev/null 2>&1                         # uses BW_CLIENTID/SECRET (no-op if logged in)
export BW_SESSION="$(bw unlock --passwordenv BW_PASSWORD --raw)"
# now: bw list items | bw get item <name> | bw create item <b64> | bw edit item <id> | bw delete item <id>
```

### bw 2026.8.0 gotchas (hit in production — follow exactly)

- **`bw create item <encodedJson>`** takes base64 of a JSON object on stdin or
  argv. The JSON **must OMIT `favorite` and `reprompt`** — `0`/`false` both
  fail with a type-mismatch crash. Minimal shape:
  `{"name":"...","type":1,"notes":"KEY=VAL","login":{"username":"x"},"fields":[],"collectionIds":[]}`
- **`bw get item <name> --json` is unsupported** — use
  `bw list items --search <name>` and filter by exact `name` in python/jq.
- **`bw delete item <id> [-p]`** — there is **no `--yes`**; `-p` permanently
  deletes (default is soft-delete to trash).
- **`bw create item` echoes the created item JSON to stdout** — if the item's
  `notes` hold a secret (a token), that secret is in your tool output. Pipe to
  `/dev/null` or mask, and never let a secret land in a commit/PR/log.
- The machine-auth (`BW_CLIENTID`/`BW_CLIENTSECRET`, a `user.*` API key) has
  **full** personal-vault access — `create`/`edit`/`delete` are destructive;
  confirm the item name before deleting.

### The notes `KEY=VAL` convention (how `vault_loader` reads items)

Several items are consumed at boot by `/app/ffm/vault_loader.sh` (groups in
`BW_VAULT_ONLY`): their **notes** hold `KEY=VAL` lines, one per line, `#`
comments and blank lines skipped. Current contract:

| item name | group | notes/fields → env |
|---|---|---|
| `opencode-cloudflare` | `cf` | notes `CLOUDFLARE_ACCOUNT_ID=` / `CLOUDFLARE_API_KEY=` |
| `cloudflare-tunnels` (folder `cloudflare`) | `cf-tunnels` | custom **fields** `account-id`/`read-all`/`write-all` → `CF_ACCOUNT_ID`/`CF_API_TOKEN_READ`/`CF_API_TOKEN_WRITE` |
| `github-fleet-token` | `gh` | notes `GH_FLEET_TOKEN=` (the agent git-push credential) |
| `provider-keys` | `llm` | notes `NVIDIA_API_KEY=`/`OPENROUTER_API_KEY=`/`MISTRAL_API_KEY=` |

To rotate a CF token or the gh token: edit the item's notes/field, then
`docker restart dsh-ffm` (vault reload is boot-only). The container's
`ffm-dsh` volume keeps the bw-cli login state, so a restart re-unlocks fast.

## 5. GitHub / self-modification (pointer)

`GH_FLEET_TOKEN` (in env) is the agent's git-push credential for
`/data/dsh/repo`. The edit→commit→push→auto-redeploy loop is documented in the
`ffm-operations` skill's **self-modification** reference — load that for code
changes. This skill covers the *infrastructure* around it (the tunnel/DNS/VM
that the deployment rides on), not the repo edit loop itself.

## 6. Safety & scope

- This agent is **public-internet-exposed** (PIN-gated at `/pin`). A tunnel
  ingress or DNS change affects real public services — confirm the target
  and effect before writing; removing an ingress takes a service offline.
- DNS writes via the CF API are logged on the Cloudflare side; the write token
  is `write-all` — scope your calls, never log the token.
- The VM is **ephemeral**: a fresh `fleet-runner.yml` spin means a fresh VM,
  a fresh tunnel connector, a fresh `/root/.cloudflared/config.yml` (only the
  `fleet-runner.yml`-seeded base ingress) — any host you added on a previous
  VM is gone and must be re-added. The `dsh-ffm-deploy` workflow re-asserts the
  `dsh-ffm` ingress on each deploy; other custom hosts are NOT re-asserted.
- Rate-limit your CF API calls; the zone has many records — page if needed.
