#!/usr/bin/env bash
# grid/scripts/_lib.sh — shared helpers for every operator script in this dir.
# Source from each wrapper:
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   . "$SCRIPT_DIR/_lib.sh"
#
# Provides:
#   resolve_repo_root   — REPO_ROOT (absolute), REPO_NAME, cd into it.
#   require_cmd         — fail fast if a binary is missing.
#   require_file        — fail fast if a file is missing.
#   venv_python         — path to .venv-ft/bin/python (the freqtrade venv).
#   freqtrade_bin       — path to .venv-ft/bin/freqtrade.
#   grid_dev            — path to grid/dev.
#   log_info / log_warn / log_err — timestamped lines.
#   run                 — run a command with timeout + exit-code capture.
#   ping_port           — `pong` from a freqtrade api_server, exit 0/1.
#   slot_list           — print the canonical 4-slot pairs.
#
# Conventions:
#   - All scripts are bash 3.2+ compatible (macOS default).
#   - Every script exits non-zero on any failure (set -e at the top).
#   - Logging is to stdout/stderr with timestamps so cron captures them.

set -euo pipefail

# Resolve REPO_ROOT by walking up from SCRIPT_DIR until we find a .git
# directory (the freq-fleet-runner checkout). Falls back to the cwd if
# no .git is found (e.g. when scripts are vendored into another path).
resolve_repo_root() {
  local start="${1:-$PWD}"
  local dir="$start"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if [[ -d "$dir/.git" ]]; then
      (cd "$dir" && pwd)
      return 0
    fi
    local parent
    parent="$(dirname "$dir")"
    [[ "$parent" == "$dir" ]] && break
    dir="$parent"
  done
  # No .git ancestor — assume we're already in the repo root.
  (cd "$start" && pwd)
}

# Default REPO_ROOT lookup if the caller hasn't set it yet.
if [[ -z "${REPO_ROOT:-}" ]]; then
  REPO_ROOT="$(resolve_repo_root "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)")"
fi
GRID_DIR="$REPO_ROOT/grid"
STATE_DIR="$GRID_DIR/state"
FT_USER_DATA="$REPO_ROOT/ft_user_data"
VENV_DIR="$REPO_ROOT/.venv-ft"
VENV_PY="$VENV_DIR/bin/python"
FT_BIN="$VENV_DIR/bin/freqtrade"
GRID_DEV="$GRID_DIR/dev"
GRID_SCREEN="$GRID_DIR/screen.py"
CONSOLE_PORT="${CONSOLE_PORT:-8798}"
PB_PORT="${PB_PORT:-8290}"
SLOT_PORTS=(8191 8192 8193 8194)
SLOT_NAMES=(ft-btc ft-eth ft-sol ft-hype)
SLOT_PAIRS=(BTC/USDC:USDC ETH/USDC:USDC SOL/USDC:USDC HYPE/USDC:USDC)
SLOT_TIMEFRAMES=(1m 3m 3m 5m)

# --- log helpers ---------------------------------------------------------

_ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }
log_info() { printf '[%s] INFO  %s\n' "$(_ts)" "$*"; }
log_warn() { printf '[%s] WARN  %s\n' "$(_ts)" "$*" >&2; }
log_err()  { printf '[%s] ERROR %s\n' "$(_ts)" "$*" >&2; }

# --- dependency checks ---------------------------------------------------

require_cmd() {
  local c
  for c in "$@"; do
    if ! command -v "$c" >/dev/null 2>&1; then
      log_err "missing required command: $c"
      exit 127
    fi
  done
}

require_file() {
  local f
  for f in "$@"; do
    if [[ ! -e "$f" ]]; then
      log_err "missing required path: $f"
      exit 1
    fi
  done
}

# --- ping / health -------------------------------------------------------

# curl --max-time, exit 0 only if HTTP 200 with the literal `pong` body.
# freqtrade's /api/v1/ping returns {"status":"pong"} on success — we
# match the substring so a wrapper on a different release can still work.
ping_port() {
  local port="$1"
  local body
  body="$(curl -sS --max-time 3 "http://127.0.0.1:${port}/api/v1/ping" 2>/dev/null || true)"
  [[ "$body" == *'"status":"pong"'* ]]
}

# --- misc ----------------------------------------------------------------

# Pretty-print a 4-tuple slot summary.
slot_summary() {
  local i
  for i in 0 1 2 3; do
    printf '%-8s %-14s TF=%s :%d\n' \
      "${SLOT_NAMES[$i]}" "${SLOT_PAIRS[$i]}" \
      "${SLOT_TIMEFRAMES[$i]}" "${SLOT_PORTS[$i]}"
  done
}
