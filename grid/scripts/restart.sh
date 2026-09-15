#!/usr/bin/env bash
# grid/scripts/restart.sh — full restart (stop, start, verify).
#
# Cycles the stack in one command: stops every component, waits for
# ports to clear, then starts everything back up and waits for the
# engines to respond on /api/v1/ping.
#
# Usage:
#   ./grid/scripts/restart.sh
#   ./grid/scripts/restart.sh --wait-secs 30   # wait N seconds per port
#
# Exit codes:
#   0   stack restarted and all engines are pinging
#   1   a port failed to clear after stop
#   2   an engine failed to come back up

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_lib.sh
. "$SCRIPT_DIR/_lib.sh"

require_file "$GRID_DEV" "$VENV_PY"

WAIT_SECS=15
for arg in "$@"; do
  case "$arg" in
    --wait-secs)
      shift
      WAIT_SECS="${1:-15}" ;;
    --help|-h)
      sed -n '2,18p' "$0"; exit 0 ;;
    *)
      log_warn "ignoring unknown arg: $arg" ;;
  esac
done

log_info "stopping stack"
"$GRID_DEV" stop

log_info "waiting up to ${WAIT_SECS}s for ports to clear"
wait_secs=0
while (( wait_secs < WAIT_SECS )); do
  busy=0
  for p in "${SLOT_PORTS[@]}" "$CONSOLE_PORT" "$PB_PORT"; do
    if ping_port "$p" 2>/dev/null; then
      busy=1
      break
    fi
  done
  if (( busy == 0 )); then
    log_info "ports clear after ${wait_secs}s"
    break
  fi
  sleep 1
  wait_secs=$((wait_secs + 1))
done
if (( wait_secs >= WAIT_SECS )); then
  log_err "ports still busy after ${WAIT_SECS}s — refusing to restart"
  exit 1
fi

log_info "starting stack"
"$GRID_DEV" start

log_info "waiting up to ${WAIT_SECS}s for engines to ping"
wait_secs=0
all_ok=0
while (( wait_secs < WAIT_SECS )); do
  ok=0
  for p in "${SLOT_PORTS[@]}"; do
    if ping_port "$p"; then
      ok=$((ok + 1))
    fi
  done
  if (( ok == ${#SLOT_PORTS[@]} )); then
    all_ok=1
    break
  fi
  sleep 1
  wait_secs=$((wait_secs + 1))
done

if (( all_ok != 1 )); then
  log_err "not all engines responded after ${WAIT_SECS}s — check grid/dev logs"
  exit 2
fi

log_info "stack restarted cleanly:"
"$GRID_DEV" status | sed 's/^/    /'
