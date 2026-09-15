#!/bin/bash
# grid-ledger-sync launcher for launchd. Changes CWD to the repo and
# runs `grid/dev ledger-sync`. Stdout/stderr are tee'd into the log
# file the plist declares (launchd's StandardOutPath then captures the
# rest). Idempotent: the ledger CLI is read-only against the FT DBs
# and atomic-write against state/reliability.json.
set -euo pipefail
cd /Volumes/ExMac/code/grid/0/freqtrade
exec ./grid/dev ledger-sync