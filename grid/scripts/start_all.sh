#!/usr/bin/env bash
# grid/scripts/start_all.sh — bring up the full standalone grid stack.
#
# Wraps `grid/dev start` with sanity checks: verifies .venv-ft exists,
# verifies the freqtrade binary resolves, prints the slot summary
# before/after, and tails the engine status when something is slow.
#
# Usage:
#   ./grid/scripts/start_all.sh                 # full stack
#   ./grid/scripts/start_all.sh --no-console    # no mission console
#   ./grid/scripts/start_all.sh --no-ft         # pocketbase + console only
#
# Exit codes:
#   0   stack is up (or partially up — see script output)
#   1   dependency missing
#   2   grid/dev start failed
#
# Cron-friendly: stdout/stderr is timestamped; safe to run repeatedly
# (start_all.sh is idempotent — re-running on an already-up stack
# returns the existing processes, see grid/dev restart semantics).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_lib.sh
. "$SCRIPT_DIR/_lib.sh"

require_file "$GRID_DEV" "$VENV_PY"

log_info "starting standalone grid stack (repo=$REPO_ROOT)"
log_info "slot plan:"
slot_summary | sed 's/^/    /'

# Pass through any flags the caller gave us — start_all.sh mirrors
# grid/dev's argument surface so the user can keep muscle memory.
EXTRA=()
for arg in "$@"; do
  case "$arg" in
    --no-console|--no-ft|--help|-h)
      EXTRA+=("$arg") ;;
    *)
      log_warn "ignoring unknown arg: $arg (try --no-console or --no-ft)" ;;
  esac
done

if ! "$GRID_DEV" start "${EXTRA[@]}"; then
  log_err "grid/dev start failed — last 40 lines of ft-btc log:"
  tail -n 40 "$STATE_DIR/ft_fleet/ft-btc/freqtrade.log" 2>/dev/null \
    | sed 's/^/    /' || true
  exit 2
fi

log_info "stack up — health check:"
"$GRID_DEV" status | sed 's/^/    /'
log_info "open the console: http://127.0.0.1:${CONSOLE_PORT}/#fleet"
