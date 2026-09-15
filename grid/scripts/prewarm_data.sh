#!/usr/bin/env bash
# grid/scripts/prewarm_data.sh — pre-download candles for all 4 slots.
#
# Solves the slow first-boot problem documented in
# grid/docs/reset-2026-09-15-lowertf.md: on a fresh tree the 4 engines
# each pull 90 days of lower-TF candles from Hyperliquid on first
# start, all from the shared HL rate budget. Running this script BEFORE
# start_all.sh fills ft_user_data/data/ with the needed feather files
# so the engines warm up instantly.
#
# Defaults: 90 days, 1m + 3m + 5m + 15m candles, all 4 slot pairs,
# futures-only (the swap perp grid never touches spot/funding rates).
# Bump --days to 180 if your analysis needs longer lookback; the LLM
# swarm still calls multi_tf_pack() with its bounded windows, but the
# engines' startup_candle_count benefits from depth.
#
# Usage:
#   ./grid/scripts/prewarm_data.sh                   # 90d, 1m+3m+5m+15m
#   ./grid/scripts/prewarm_data.sh --days 180        # longer history
#   ./grid/scripts/prewarm_data.sh --timeframes 1m 5m 15m
#   ./grid/scripts/prewarm_data.sh --exchange binance --pairs BTC/USDT:USDT
#       # ad-hoc smoke against any exchange; useful for verifying the
#       # pipeline before pointing at the live slot pairs.
#
# Exit codes:
#   0   all fetches finished; feathers present in ft_user_data/data/
#   1   .venv-ft/bin/freqtrade missing or freqtrade invocation failed
#   2   no feather files appeared after the run (silent fetch failure)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_lib.sh
. "$SCRIPT_DIR/_lib.sh"

require_file "$FT_BIN" "$FT_USER_DATA/config.json"

DAYS=90
TIMEFRAMES=(1m 3m 5m 15m)
EXCHANGE=hyperliquid
PAIRS=("${SLOT_PAIRS[@]}")
TRADING_MODE=futures
CANDLE_TYPES=(futures mark)

# arg parse (minimal, just what the doc above documents)
while (( $# > 0 )); do
  case "$1" in
    --days)
      shift; DAYS="${1:?--days requires an integer}" ;;
    --timeframes)
      shift
      TIMEFRAMES=()
      while (( $# > 0 )) && [[ "$1" != --* ]]; do
        TIMEFRAMES+=("$1"); shift
      done ;;
    --exchange)
      shift; EXCHANGE="${1:?--exchange requires a name}" ;;
    --pairs)
      shift
      PAIRS=()
      while (( $# > 0 )) && [[ "$1" != --* ]]; do
        PAIRS+=("$1"); shift
      done ;;
    --help|-h)
      sed -n '2,30p' "$0"; exit 0 ;;
    *)
      log_warn "ignoring unknown arg: $1" ;;
  esac
  shift || true
done

log_info "prewarm: ${EXCHANGE} ${DAYS}d, TFs=${TIMEFRAMES[*]}, pairs=${#PAIRS[@]}"
log_info "writing to: $FT_USER_DATA/data"

# Build the freqtrade CLI. Use --trading-mode=futures + --candle-types
# futures,mark so the engine's pair_candles probe finds the bars it
# expects on first start. --prepend makes the fetch additive across
# runs (no full re-download when the script is run daily).
TF_ARGS=()
for tf in "${TIMEFRAMES[@]}"; do TF_ARGS+=("$tf"); done
PAIR_ARGS=()
for p in "${PAIRS[@]}"; do PAIR_ARGS+=("$p"); done

CT_ARGS=()
for ct in "${CANDLE_TYPES[@]}"; do CT_ARGS+=("$ct"); done

# freqtrade exits non-zero if even one pair fails — we capture, log,
# and continue so a single bad symbol doesn't abort the whole warm.
log_info "running freqtrade download-data (this can take a few minutes)..."
if ! "$FT_BIN" download-data \
    --userdir "$FT_USER_DATA" \
    --exchange "$EXCHANGE" \
    --trading-mode "$TRADING_MODE" \
    --candle-types "${CT_ARGS[@]}" \
    --pairs "${PAIR_ARGS[@]}" \
    --timeframes "${TF_ARGS[@]}" \
    --days "$DAYS" \
    --prepend 2>&1 | tee /tmp/prewarm.log; then
  log_warn "freqtrade download-data exited non-zero — checking what landed"
fi

# Verify: at least one futures feather per (pair, tf) combo should exist.
total=0; landed=0
for p in "${PAIRS[@]}"; do
  slug=$(echo "$p" | tr '/' '_' | tr -d ':')
  for tf in "${TIMEFRAMES[@]}"; do
    total=$((total + 1))
    f="$FT_USER_DATA/data/${EXCHANGE}/${TRADING_MODE}/${slug}-${tf}-futures.feather"
    if [[ -s "$f" ]]; then
      landed=$((landed + 1))
      log_info "  ok  $f ($(stat -f%z "$f" 2>/dev/null || stat -c%s "$f") bytes)"
    else
      log_warn "  missing $f"
    fi
  done
done

log_info "prewarm complete: $landed/$total feather files present"
if (( landed == 0 )); then
  log_err "no feather files landed — investigate /tmp/prewarm.log"
  exit 2
fi
