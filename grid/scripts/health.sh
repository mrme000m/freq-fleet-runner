#!/usr/bin/env bash
# grid/scripts/health.sh — probe every grid component and report.
#
# Checks (in order):
#   1. pocketbase  on :8290  — /api/health must return 200
#   2. console     on :8798  — / must return 200 (or a redirect; not 5xx)
#   3. each engine on :8191-:8194 — /api/v1/ping must return {"status":"pong"}
#   4. candle freshness — for each engine, the last pair_candles ts on
#      the slot's TF must be within 3× the TF interval (so a 1m slot is
#      stale after 3 min, a 5m slot after 15 min). Uses the engine's
#      own api_server with the slot's credentials (loaded from
#      state/ft_fleet/registry.json).
#
# Output is a one-line per-component "ok" / "DOWN" / "stale" verdict
# followed by a single summary line. Exit code:
#   0  every component is healthy and candle-fresh
#   1  at least one component is DOWN or stale
#   2  registry.json missing (stack never started)
#
# Suitable for cron / monitoring. Use --json for machine-parseable
# output (`{"pocketbase":"ok","console":"ok","ft-btc":"ok",...}`).
#
# Usage:
#   ./grid/scripts/health.sh                  # human-readable
#   ./grid/scripts/health.sh --json           # machine-readable
#   ./grid/scripts/health.sh --quiet          # exit code only

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_lib.sh
. "$SCRIPT_DIR/_lib.sh"

MODE=human
while (( $# > 0 )); do
  case "$1" in
    --json) MODE=json ;;
    --quiet) MODE=quiet ;;
    --help|-h)
      sed -n '2,32p' "$0"; exit 0 ;;
    *) log_warn "ignoring unknown arg: $1" ;;
  esac
  shift
done

REGISTRY="$STATE_DIR/ft_fleet/registry.json"
if [[ ! -s "$REGISTRY" ]]; then
  log_err "registry not found at $REGISTRY — start the stack first"
  exit 2
fi

# Per-TF max-staleness in seconds (3 × the candle interval).
stale_secs_for() {
  case "$1" in
    1m) echo 180 ;;
    3m) echo 540 ;;
    5m) echo 900 ;;
    15m) echo 2700 ;;
    *) echo 600 ;;
  esac
}

# --- pocketbase ---------------------------------------------------------
pb_state="DOWN"
if curl -sS --max-time 3 "http://127.0.0.1:${PB_PORT}/api/health" \
   | grep -q '"code":200\|"status":"ok"'; then
  pb_state="ok"
fi

# --- console ------------------------------------------------------------
console_state="DOWN"
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 \
       "http://127.0.0.1:${CONSOLE_PORT}/" || echo 000)
if [[ "$code" =~ ^[23] ]]; then
  console_state="ok"
fi

# --- engines + candle freshness -----------------------------------------
# bash 3.2 (macOS default) has no `declare -A`, so we stash each slot's
# state in a per-bot tmpfile under /tmp/grid-health.<bot>.<pid> and
# read them back at emit time.
HEALTH_TMP="$(mktemp -d -t grid-health.XXXXXX)"
trap 'rm -rf "$HEALTH_TMP"' EXIT

slot_state_for() { cat "$HEALTH_TMP/$1" 2>/dev/null || echo "missing"; }

overall_ok=1

# Registry is JSON; we use python (already required for freqtrade) so
# we don't need jq as an external dep.
read_registry() {
  "$VENV_PY" - "$REGISTRY" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    reg = json.load(f)
for code, entry in reg.get("instances", {}).items():
    print(f"{code}\t{entry.get('port')}\t{entry.get('username','')}\t{entry.get('password','')}\t{entry.get('pair','')}\t{entry.get('timeframe','')}")
PY
}

now=$(date +%s)

while IFS=$'\t' read -r bot port user pass pair tf; do
  [[ -z "$bot" ]] && continue
  if ! ping_port "$port"; then
    echo "DOWN" > "$HEALTH_TMP/$bot"
    overall_ok=0
    continue
  fi
  # Probe last candle freshness via pair_candles?limit=1.
  pair_enc=$(printf '%s' "$pair" | "$VENV_PY" -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.stdin.read().strip(),safe=""))')
  body=$(curl -sS --max-time 4 -u "${user}:${pass}" \
         "http://127.0.0.1:${port}/api/v1/pair_candles?pair=${pair_enc}&timeframe=${tf}&limit=1" \
         2>/dev/null || true)
  # Freqtrade returns either a dict with "candles" or a list. The first
  # bar's "date" / "time" / "open_time" field carries the epoch ms.
  last_ts_ms=$("$VENV_PY" - <<PY 2>/dev/null || echo 0
import json, sys
try:
    raw = """$body"""
    obj = json.loads(raw)
    if isinstance(obj, dict):
        candles = obj.get("candles") or obj.get("data") or []
    elif isinstance(obj, list):
        candles = obj
    else:
        candles = []
    if candles:
        c = candles[0]
        for k in ("date","time","open_time","t","timestamp"):
            if k in c and isinstance(c[k], (int, float)):
                print(int(c[k] if c[k] < 10**12 else c[k]/1000))
                sys.exit(0)
except Exception:
    pass
print(0)
PY
)
  if [[ -z "$last_ts_ms" || "$last_ts_ms" == "0" || "$last_ts_ms" == "None" ]]; then
    echo "ok (no candle probe)" > "$HEALTH_TMP/$bot"
  else
    last_s=$((last_ts_ms / 1000))
    age=$(( now - last_s ))
    max_age=$(stale_secs_for "$tf")
    if (( age > max_age )); then
      echo "stale (${age}s > ${max_age}s)" > "$HEALTH_TMP/$bot"
      overall_ok=0
    else
      echo "ok (${age}s)" > "$HEALTH_TMP/$bot"
    fi
  fi
done < <(read_registry)

# --- emit ---------------------------------------------------------------
case "$MODE" in
  json)
    out="{\"pocketbase\":\"${pb_state}\",\"console\":\"${console_state}\""
    for bot in "${SLOT_NAMES[@]}"; do
      st=$(slot_state_for "$bot")
      # JSON-escape backslashes and quotes inside `st`.
      esc=$(printf '%s' "$st" | "$VENV_PY" -c 'import json,sys;print(json.dumps(sys.stdin.read().rstrip("\n"))[1:-1])')
      out+=",\"${bot}\":\"${esc}\""
    done
    out+="}"
    printf '%s\n' "$out" ;;
  quiet)
    exit $(( 1 - overall_ok )) ;;
  *)
    printf '%-12s %s\n' "pocketbase" "$pb_state"
    printf '%-12s %s\n' "console"    "$console_state"
    for bot in "${SLOT_NAMES[@]}"; do
      printf '%-12s %s\n' "$bot" "$(slot_state_for "$bot")"
    done
    if (( overall_ok == 1 )); then
      log_info "all components healthy"
    else
      log_warn "one or more components need attention"
    fi
    ;;
esac

exit $(( 1 - overall_ok ))
