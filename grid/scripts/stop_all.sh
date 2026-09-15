#!/usr/bin/env bash
# grid/scripts/stop_all.sh — tear down the standalone grid stack.
#
# Wraps `grid/dev stop` with optional keep-flags for partial stops.
# By default every component is stopped (4 engines + console + PB).
#
# Usage:
#   ./grid/scripts/stop_all.sh                  # full stop
#   ./grid/scripts/stop_all.sh --keep-console   # leave console up
#   ./grid/scripts/stop_all.sh --keep-pb        # leave pocketbase up
#   ./grid/scripts/stop_all.sh --keep-ft        # leave engines running
#
# Flags compose — `./grid/scripts/stop_all.sh --keep-console --keep-pb`
# stops just the 4 engines.
#
# Exit codes mirror grid/dev stop: 0 on clean stop, non-zero if any
# process refuses to die within the grace window (see STOP_GRACE_S).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_lib.sh
. "$SCRIPT_DIR/_lib.sh"

require_file "$GRID_DEV"

log_info "stopping standalone grid stack"
"$GRID_DEV" stop "$@"
log_info "stop complete"
