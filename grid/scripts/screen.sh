#!/usr/bin/env bash
# grid/scripts/screen.sh — rank Hyperliquid USDC perps by ATR×volume.
#
# Thin wrapper around grid/screen.py. Prints the ranked table to
# stdout and writes grid/state/screen_cache.json (consumed by
# `grid/dev rotate` and the LLM swarm).
#
# The screen anchors on 5m candles (post-2026-09-15 reset); the LLM
# swarm still calls multi_tf_pack() on candidates it actually
# evaluates, so this is the cheap pre-filter.
#
# Usage:
#   ./grid/scripts/screen.sh                # top 30, fetch 30
#   ./grid/scripts/screen.sh --top 50       # wider universe
#   ./grid/scripts/screen.sh --fetch 10     # smaller candle fetch (faster)
#   ./grid/scripts/screen.sh --out /tmp/cache.json
#       # write the cache somewhere else (useful for diffing)
#
# Exit codes mirror grid/screen.py: 0 on success, non-zero if HL is
# unreachable AND no cache was written. We never destroy an existing
# cache on failure.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_lib.sh
. "$SCRIPT_DIR/_lib.sh"

require_file "$GRID_SCREEN" "$VENV_PY"

DEFAULT_OUT="$STATE_DIR/screen_cache.json"
TOP=30
FETCH=30
EXTRA=()

while (( $# > 0 )); do
  case "$1" in
    --top)
      shift; TOP="${1:?--top requires an integer}" ;;
    --fetch)
      shift; FETCH="${1:?--fetch requires an integer}" ;;
    --out)
      shift; DEFAULT_OUT="${1:?--out requires a path}" ;;
    --help|-h)
      sed -n '2,18p' "$0"; exit 0 ;;
    *)
      EXTRA+=("$1") ;;
  esac
  shift || true
done

log_info "screen: top=$TOP fetch=$FETCH → $DEFAULT_OUT"
if ! "$VENV_PY" "$GRID_SCREEN" --top "$TOP" --fetch "$FETCH" \
       --out "$DEFAULT_OUT" "${EXTRA[@]}"; then
  log_err "screen.py failed — see output above"
  exit 1
fi

# Summarize what landed so the operator sees the head of the table
# without having to jq the cache.
if [[ -s "$DEFAULT_OUT" ]]; then
  pairs=$(grep -c '"pair":' "$DEFAULT_OUT" || true)
  log_info "wrote $pairs pairs to $DEFAULT_OUT"
fi
