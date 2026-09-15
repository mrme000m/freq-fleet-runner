# Console review — gaps & misalignments found and fixed (2026-09-15)

Scope: `grid/console/` end-to-end — `server.py` ↔ `static/app.js` ↔
`static/index.html` ↔ `test_upgrade.py`, cross-checked against the LIVE
runtime (console :8798, 4 dry-run engines :8191-8194, PB :8290). Every fix
below was verified live after a console-only restart
(`grid/dev stop --keep-ft --keep-pb` → `start --no-ft`); engines kept
trading throughout.

## Second pass (same day, later) — new findings

The first pass (§1-8 below) fixed the ctl-plane fusion, sparkline interval
drift, TF lies, PnL sawtooth and freshness bugs. A second full pass found
what was still off:

1. **"Halt mission" was a placebo** — it wrote `grid/KILL`, the WT-era
   brain's halt flag, but nothing in the standalone stack reads that file
   (`grid/dev` never does; `grep` confirms no consumer). The confirm
   dialog even claimed "the daemon halts at the next loop tick". Retired
   the button and the always-green "KILL clear" statusbar chip; a KILL
   file now surfaces as an honest *legacy artifact* banner + warn chip +
   a Clear-KILL button (in the controls card and the banner) only when
   the file actually exists. `/api/ctl/kill|unkill` stay for API compat
   (pinned by `test_ctl_kill_is_local_file_only`).
2. **Readiness LLM cells all read "down" while pings succeeded** —
   `_llm_env_keys()` hand-parsed `state/llm.env` without stripping the
   `export ` prefix, so `"export MISTRAL_API_KEY"` never matched the
   candidate key. The sidecar's canonical parser (`_llm_sidecar()`)
   handles it; the probe now reuses it. Regression test added.
3. **Instance cards showed a stale channel band next to a "live" badge**
   — `renderFleetInstances`' change-signature omitted
   `channel_live.low/high/mid` (+`api_backoff`), so a drifting ATR band
   with unchanged grid count never re-rendered the row it labeled "live".
   Sig now covers every painted field.
4. **The Optimizer tab presented the frozen WT-era ledger as live
   context** — when the live ledger is empty, the server silently falls
   back to `state/reliability.json` (the panel copy even said "the live
   reliability ledger"). The payload now declares
   `fast.reliability_source` and the card carries a provenance badge
   ("live · trades DBs" / "frozen WT-era file").
5. **The Config tab claimed a daemon reads config.yaml at startup** —
   nothing in the standalone stack does (fleet params are `grid/dev`
   constants + `GridStrategy.json`; ladder thresholds are constants in
   `grid/reliability/ledger.py`). Tab copy, post-save banner (and its
   placebo "Restart daemon" button) and the freshness banner ("fresh" at
   9.6h old) are all honest now.
6. **Reliability and Run-cards tabs never auto-refreshed** — despite the
   Reliability copy saying the ledger "fills live from the trades DBs".
   Both poll every 30s now (Config/LLM stay click-only — a poll would
   clobber in-progress edits).
7. **PnL header passed one slot's wallet off as the fleet's** —
   "dry-run wallet $149.89" was the first running instance's balance.
   Now the fleet sum with a `n/N reporting` note for engines in REST
   back-off.
8. **Feedless symbols retried forever and spun the market modal** —
   HYPE (no Binance/TV pair) re-fetched every 5s poll and showed a 12s
   spinner ending in a misleading "no chart data cached … yet". Failures
   are remembered feedless for 5min (success clears), the modal says the
   honest thing immediately, a fetch *timeout* no longer strands the
   spinner (unhandled rejection fixed), and a card re-render repaints
   the no-feed note (regression guard against the sig fix of §3).
9. **`renderLlmBrains` re-fetched + re-rendered every 5s poll** — now
   60s-throttled + in-flight-guarded, matching the server's 60s ping
   cache.
10. **UI/UX + a11y**: PnL canvas now draws at the chart cell's rendered
    width (was a fixed 360px in a much wider cell); sparklines are
    keyboard-openable (role=button, tabIndex, Enter/Space, aria-label —
    the CSS focus outline already existed); tabs use the WAI-ARIA roving
    tabindex; engine-event kinds got the colors the WT-era kinds had
    (feed + decisions badges: open=teal, fill=violet, close=amber);
    channel prices use magnitude-aware `fmtPrice` (0-decimals was lossy
    on low-price pairs) with a null-guard; `dev clean` no longer
    force-reloads the page (the console doesn't restart for clean);
    PB chip says "pocketbase up (legacy)" instead of "PB journal up".
11. **Dead code pruned**: `estimateCloseBy`, `heldFor` (WT-era bot-card
    helpers), `blockedByBadge` (WT-era rec tables), `confirmDialog`'s
    never-used `checkbox2`. `console/README.md` still described the
    WT-era console (daemon-ctl proxy, launchd, WT accounts,
    `../tests/test_console.py`) — rewritten to the standalone reality.

Tests: 14/14 (`test_optimizer_reliability_source_declared` +
`test_llm_env_keys_parses_export_prefix` added; suite renumbered).
Verified live post-restart in a real browser: statusbar chips (no placebo
KILL chip, honest PB chip), readiness LLM cells up (Mistral/OpenRouter/
NVIDIA present, others honestly down), fleet wallet "$449 (3/4
reporting)", channel rows with full-precision prices, BTC/ETH/SOL
sparklines painted + HYPE honest no-feed note, Optimizer provenance badge
"frozen WT-era file", Config honest copy + "config.yaml — last write"
banner, colored decisions badges, responsive PnL canvas at 793px.
Engines traded throughout (fills grew during the pass).

## First pass — original findings

## 1. The console had fused to the M3 companion daemon (critical)

`_ctl()`/`_ctl_cached()` proxy `:8799`. The WT-era brain that used to own
that port was retired at the 2026-09-14 pivot and nothing in the standalone
stack serves it — but on 2026-09-15 the **M3 companion repo's daemon**
(cwd `…/tradingview/go/agents/grid-autonomy`) came up on `:8799` and
answers with the legacy `/status` shape. The console silently fused to a
foreign system:

- `/api/overview`'s readiness probe took the WT-era branch
  (`readiness.source: "daemon-ctl"`) and the Readiness strip rendered the
  companion daemon's diagnostics as ours: "0 instances", strategy files
  "missing", their LLM env keys. Verified live before the fix.
- Every 5s poll read the foreign daemon's `/status`.
- The **Halt-mission button POSTed `/kill` to their daemon** instead of
  writing the local `KILL` file (the local-file fallback only fired when
  the port was dead).
- `/api/optimizer` carried a dead `_ctl("/optimizer")` round-trip.

**Fix** — `CTL_RETIRED = True` in `server.py`: `_ctl()` short-circuits, all
ctl callers degrade honestly (`/api/ctl/kill` now always writes the LOCAL
`KILL` file; rescreen/optimize/reliability/rotate return a 502 "ctl
retired"; `/api/status`+`/api/observe` fail-soft; `/api/optimizer` builds
its payload purely from workspace artifacts). `overview_payload` also
passes `None` to `_readiness` unless a real status answered — the
retired-error dict is truthy and must never masquerade as one (this
regression was caught live during verification). `grid/dev status` itself
declares "brain (autonomy): NOT in this repo" — the console now matches.

## 2. Fleet sparklines never painted (interval-key drift)

The 2026-09-15 lower-TF reset bumped `fetchChart`'s default interval to
`5m` (and the server now accepts `1m|3m|5m|15m|1h|4h|1d`), but the two
cache READERS still looked up the `:1h` key — `renderSlotSparklines` and
the market modal's first paint always missed, so every card showed
"chart loading…" forever and the modal always took the spinner path.

**Fix** — one `SPARK_INTERVAL = "5m"` constant feeds the fetch default,
both cache lookups, the sparkline tooltip, the modal spinner/captions/aria
(copy now derives from the served `interval` field, so captions can't lie
again).

Two follow-on gaps found while verifying the fix live:

- **One hung fetch held the whole board hostage.** `loadSlotCharts` awaited
  `Promise.allSettled` of ALL symbols before painting; `HYPEUSDC` maps to
  `BINANCE:HYPEUSDT` (no Binance feed) and burned the full 30s tvcli
  timeout each round. Now each fetch paints its own sparkline as it
  lands, and a failed symbol renders an honest
  "no market feed — tvcli has no Binance pair for HYPEUSDC" note.
- **Chart failures were never cached server-side**, so the no-feed symbol
  re-hung tvcli for 30s on every 5s poll. `_chart_bars` now caches the
  fail-soft error payload on the same 60s TTL as successes.

Known limitation (not a console bug): HYPE has no TradingView/Binance pair,
so its *sparkline* has no market feed. The slot still shows its live
channel, wallet and trades (served from the engine, not tvcli). Future
option: sparkline fallback to the engine's own `pair_candles` REST for
Hyperliquid-native symbols.

## 3. Instance cards lied about the timeframe

The registry carries a per-slot `timeframe` (BTC=1m, ETH/SOL=3m, HYPE=5m
since the reset) but `_FT_SAFE_FIELDS` didn't whitelist it and the card
hardcoded "· 1h ·". Fixed both: the field crosses the /api boundary and
the card renders the real TF. The Start-mission dialog copy ("GridStrategy
on BTC/USDC:USDC 1h") was equally stale → now describes the actual fleet.

## 4. PnL timeline sawtoothed between bots

`pnl_payload`'s trades-DB branch emitted one cumulative series **per bot**
under the shared `fleet` key; the frontend chart plots a single fleet line,
so it zigzagged between each bot's own running sum. Now the close events
from all instances merge into one fleet-cumulative series plus a single
trailing live point (fleet realized + summed open-position marks).
Regression-tested in `test_upgrade.py` (`test_pnl_timeline_is_fleet_cumulative`).

## 5. Logs pane freshness banner was fabricated

`loadLogs` computed the banner's timestamp from `new Date(s.path)` —
parsing the PATH STRING as a date → NaN → `-Infinity` → the LIVE logs pane
was mislabeled "Stale snapshot — brain closed". The server now sends
`mtime` per source (`_log_source`), and the banner shows real freshness
("Last written … 2s ago", verified live).

## 6. Reliability freshness ignored SQLite WAL

`_reliability_live` stat'ed only the main `tradesv3.dryrun.sqlite`
(checkpoint-time mtime) while live writes land in the `-wal` sidecar —
the pane claimed "last trade activity 125m ago" while the fleet was
filling orders. Freshness now tracks db + `-wal` + `-shm` (verified live:
age dropped 7530s → 60s).

## 7. Smaller alignment fixes

- **Tuned-params banner**: fetched only in the no-fleet branch (stale all
  session in the normal path) and could render "band_atr undefined" on a
  half-loaded payload. Now refreshed (60s-throttled) from the banner
  renderer itself, and only accepted when both numbers parse. Verified
  live: "band_atr 4.2 / step_factor 1" (the M5 values — the old hardcoded
  copy still said step_factor 0.21).
- **Status badges**: the fleet card / run-card / statusbar / PnL header
  used binary `api_ok` ("running"/"down"), painting a warming-up or
  429-backing-off engine as "down". All now use the server's live-derived
  three-state `status` (+`api_backoff` tooltip).
- **Freshness badges** prefer the server's `frozen`/`frozen_at` verdict
  (derived from `state/engine.json.since`, which the 2026-09-15 reset
  bumped to 09:31Z) over the hardcoded 2026-09-14 epoch; the banner date
  is now dynamic.
- **Fleet summary**: "Engine uptime" showed a count (label lied) →
  "Engines running 4 / 4"; "Pair" showed only the first slot's pair → all
  four.
- **Optimizer "Applied" table**: empty-state row colspan 9 in an 8-column
  table.
- **index.html**: brand still read "grid/autonomy" after the grid-fleet
  rename; dead `#rel-freshness` slot (never written) and empty
  `#opt-trackers` card removed.
- **Stale module docstring** rewritten to the actual API (ctl retired,
  chart intervals, merged PnL, dev-script lifecycle).

## 8. Test suite realignment

`console/test_upgrade.py` predated the pivot-era payload contracts — 3 of
its 5 failures existed BEFORE this pass (it still expected the removed
`state.json` PnL fallback, an `error` key on the optimizer's N/A payload,
and the file ledger under `archetypes`; `ledger_age_h`/`stale` keys no
longer exist anywhere). Updated to the current contracts and extended:

- `test_pnl_timeline_is_fleet_cumulative` — two synthetic sqlite DBs, one
  merged fleet series (regression for §4)
- `test_ctl_kill_is_local_file_only` — kill writes the LOCAL KILL file and
  unkill clears it (regression for §1)
- retired-ctl expectations (`ctl retired …` 502/error copy)
- `test_upgrade.py` now isolates `KILL_FILE`, `_REL_CACHE`, `_GEOM_CACHE`
  per test (no pollution of the real `grid/KILL`).

**12/12 pass** (`python3 -m unittest discover -s console -p
"test_upgrade.py"`).

## Verification summary (live, post-fix)

- Readiness strip: `source: grid-dev`, 4 engine cells up, venv/strategy/PB
  all green — no companion-daemon data anywhere.
- Sparklines paint within seconds (BTC/ETH/SOL deltas live); HYPE shows
  the honest no-feed note; tuned banner shows the real M5 params.
- PnL header + single fleet timeline point; per-tab renders verified in a
  real browser (decisions 22 events, run cards 4 rows, optimizer 4 geom
  rows, config 40 fields, logs 400 lines with fresh banner).
- All four engines kept trading across the three console-only restarts.
