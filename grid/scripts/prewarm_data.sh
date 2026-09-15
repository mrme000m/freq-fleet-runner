#!/usr/bin/env bash
# grid/scripts/prewarm_data.sh — pre-download OHLCV candles via freqtrade.
#
# Best-effort history warm-up for backtests / hyperopt. On exchanges
# that support it (Binance, Bybit, OKX, Kraken, …) this populates
# ft_user_data/data/<exchange>/<market>/ with feather files so backtest
# runs don't hit the exchange at every iteration.
#
# Hyperliquid note (2026-09-15 reset): the engines on Hyperliquid do NOT
# need this script — freqtrade's `download-data` is rejected by the
# ccxt Hyperliquid implementation ("Historic data not available for
# Hyperliquid"). The live engines stream candles one bar at a time via
# the per-tick ccxt `fetch_ohlcv` path, and the strategy warms up from
# those live ticks (startup_candle_count=60 × slot TF). This script
# detects that case and exits 0 with an explanation, so it can stay in
# cron pipelines without generating noise.
#
# Usage:
#   ./grid/scripts/prewarm_data.sh                   # 4 slot pairs (HL)
#   ./grid/scripts/prewarm_data.sh --days 180        # longer history
#   ./grid/scripts/prewarm_data.sh --timeframes 1m 5m 15m
#   ./grid/scripts/prewarm_data.sh --exchange binance --pairs BTC/USDT:USDT
#       # ad-hoc fetch against any supported exchange (useful for
#       # verifying the pipeline or for backtests/hyperopt).
#
# Exit codes:
#   0   all fetches finished (or skipped on purpose, e.g. Hyperliquid)
#   1   .venv-ft/bin/freqtrade missing
#   2   freqtrade invocation failed AND no feathers were written

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
dl_log=/tmp/prewarm.log
if ! "$FT_BIN" download-data \
    --userdir "$FT_USER_DATA" \
    --exchange "$EXCHANGE" \
    --trading-mode "$TRADING_MODE" \
    --candle-types "${CT_ARGS[@]}" \
    --pairs "${PAIR_ARGS[@]}" \
    --timeframes "${TF_ARGS[@]}" \
    --days "$DAYS" \
    --prepend >"$dl_log" 2>&1; then
  # Detect the "exchange doesn't support historical OHLCV" refusal
  # pattern that Hyperliquid (and a few others) emit. This is NOT an
  # error condition for the live engines — they fetch live ticks via
  # ccxt on every candle close — so we exit cleanly rather than fail
  # the script.
  if grep -q 'does not support downloading trades or ohlcv data' "$dl_log"; then
    log_warn "${EXCHANGE} has no historical OHLCV endpoint — engines will"
    log_warn "  warm up from live ccxt ticks (startup_candle_count per slot)."
    log_warn "  No prewarm needed. Exiting 0."
    exit 0
  fi
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
  log_err "no feather files landed — investigate $dl_log"
  exit 2
fi
