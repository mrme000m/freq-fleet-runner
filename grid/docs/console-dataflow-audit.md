# Mission console — endpoint → data-source audit

Scope: `grid/console/server.py` (stdlib `ThreadingHTTPServer`, port 8798) +
`grid/console/static/app.js`. Classification is against **this** workspace
(`/Volumes/ExMac/code/grid/0/freqtrade`), runtime truth checked 2026-09-14T23:18Z.

Classes:

- **LIVE** — produced by the `grid/dev` standalone runtime (console :8798 +
  one dry-run freqtrade engine), or a currently-running in-workspace service.
- **FROZEN** — legacy WT-era file; last writer (the daemon brain) was retired
  2026-09-14T21:42:37Z (see `grid/state/engine.json` → `since` / `legacy.closed_at`).
- **DANGLING** — points at a missing file/module/service in this workspace.
- **EXTERNAL** — depends on an out-of-workspace service (network APIs, TradingView
  via tvcli).

Runtime listeners at audit time (read-only `lsof`):

| Port | Process | Status |
|---|---|---|
| 8798 | console (pid 21927) | LIVE |
| 8191 | freqtrade REST, `ft-btc` dry-run (pid 22467) | LIVE |
| 8765 | `tvcli` serve (pid 616) | LIVE |
| 8090 | PocketBase (binary + `pb.env` exist under `grid/.pocketbase/`) | **not listening** |
| 8799 | daemon ctl plane | **nothing** (brain retired) |

Workspace drift note: earlier audit context claimed `grid/state/llm.env`,
`grid/llm/`, `grid/pbclient.py`, `grid/.pocketbase/` were ABSENT. All four now
**exist** (llm.env mtime 2026-09-14T23:14Z — post-pivot, still maintained).
PocketBase itself is simply not running, so PB reads still degrade.

## 1. Endpoint table

### GET routes (dispatch: `server.py` `do_GET`, ~line 2380)

| Endpoint | Source | Classification | Consumer pane (app.js) |
|---|---|---|---|
| `/` , static | `grid/console/static/*` files | LIVE | all (shell) |
| `/api/overview` | merged: `state/state.json` (FROZEN) + ctl `/status` probe (DANGLING) + `engine.json` + `ft_fleet/registry.json` (LIVE) + PB health ping (DANGLING) + `config.yaml` (LIVE) + `reliability.json` (FROZEN) + latest rescreen card (FROZEN) + per-instance readiness pings `127.0.0.1:<port>/api/v1/ping` (LIVE) | MIXED (FROZEN-dominant) | fleet (line 316; drives statusbar, readiness strip, fleet cards, feed, screen rail, PnL strip, sparklines, LLM brains) |
| `/api/daemon` | `grid/dev status` subprocess + `ps` + `lsof :8090` (PB probe, mislabeled `ctl_reachable`) | LIVE | none directly (server-internal via overview) |
| `/api/state` | `grid/state/state.json` | FROZEN (last write 2026-09-14T21:44:08Z) | none directly (server-internal) |
| `/api/journal` | `state.json` → `journal[]` ring | FROZEN | none directly (via overview) |
| `/api/decisions?limit=` | `grid/state/decisions.jsonl` tail + `_freshness()` | FROZEN (last write 2026-09-14T21:33:58Z) | decisions (line 1807) |
| `/api/decisions/<id>` | same file, full-scan index + cohort aggregation | FROZEN | decisions drill-down (2063), fleet slot-card "decision evidence" deep-link |
| `/api/reliability` | `grid/state/reliability.json` + `_freshness()` | FROZEN (last write 2026-09-14T20:11:09Z — pre-pivot) | reliability (2875) |
| `/api/reliability/archive` | `import reliability_grid` from `grid/execution/` — **module does not exist here** → import fails → always `{"archetypes": {}}`; underlying `state/reliability_archive.json` (mtime 2026-09-08) is FROZEN | DANGLING (code) + FROZEN (data) | reliability expandable rows (2988) |
| `/api/recommendations?limit=` | PocketBase `recommendations` collection via `pbclient` / raw HTTP `:8090` → falls back to `state.json` journal events | DANGLING (PB down) → FROZEN fallback | optimizer (2299) |
| `/api/screen` | newest `state/reports/<ts>-rescreen.{json,md}` | FROZEN (newest card 20260914T210046Z) | none directly (embedded in overview screen rail) |
| `/api/optimizer` | hardcoded not-applicable payload + `_optimizer_standalone_payload()`: `state/ft_fleet/*/grid.json` (LIVE), `grid/strategies/GridStrategy.json` (LIVE), `state/reliability.json` (FROZEN), `state/decisions.jsonl` tail (FROZEN) | MIXED (honest N/A + mixed data) | optimizer (2285) |
| `/api/optimizer/swap-log` | `state.json` → `optimizer` block (trackers, swap_log, arbiter verdict) | FROZEN | optimizer (2310) |
| `/api/llm/health` | subprocess `grid/llm/provider.py --ping --json` (env from `state/llm.env`, mtime 2026-09-14T23:14Z) + provider HTTP APIs + `config.yaml` | LIVE (pings are EXTERNAL: CF/NVIDIA/OpenRouter/Mistral) | fleet LLM-brains strip (1421) |
| `/api/observe` | daemon ctl `GET /observe` on `:8799` (5s cache) | DANGLING (fail-soft 200 `{"error":"ctl unreachable"}`) | none (no app.js caller) |
| `/api/status` | daemon ctl `GET /status` on `:8799` (5s cache) | DANGLING (fail-soft) | fleet (324), optimizer (2303) |
| `/api/pnl` | PocketBase `journal` records `kind='pnl-snapshot'` (`pbclient` → raw HTTP → sidecar token) → fallback `state.json` journal | DANGLING (PB down) → FROZEN fallback (200 events) | fleet PnL timeline (1794) |
| `/api/chart?venue&symbol&interval&bars` | POST to tvcli `http://127.0.0.1:8765/fetch` (60s cache) | LIVE (tvcli upstream data is EXTERNAL: TradingView) | fleet slot sparklines (808/823) |
| `/api/position-sweeps` | `state.json` journal, `position-optimizer*` kinds | FROZEN | optimizer (2307) |
| `/api/reports` | `os.listdir(state/reports)` index + `_freshness(dir)` | FROZEN (1157 files; newest rescreen 2026-09-14T21:00:46Z, newest audit 2026-09-06) | run cards (2091) |
| `/api/reports/<stem>` | `state/reports/<stem>.json` + `.md` | FROZEN | run cards detail (2127) |
| `/api/logs?lines=&grep=` | `_log_source()`: launchd log if managed, else virtual tail of every `state/logs/*` + `state/ft_fleet/*/freqtrade.log` | LIVE (workspace-tail; `daemon-launchd.log` itself FROZEN at 21:42:42Z) | logs (3374) |
| `/api/config` | `grid/config.yaml` (parsed + `EDITABLE` whitelist) + `_freshness()` | LIVE (file editable in-place) | config (3035) |
| `/api/llm` | `state/llm.env` sidecar (presence booleans only) + process env | LIVE | config → LLM section (3157) |
| `/api/meta` | process facts, `_ctl_port()` (legacy), `engine.json` → `engine_payload()`, `ft_fleet/registry.json` | LIVE | boot header/footnote (3702) |

### POST routes (`do_POST`, ~line 2513; all require same-origin)

| Endpoint | Source | Classification | Consumer pane |
|---|---|---|---|
| `/api/ctl/rescreen` `/optimize` `/reliability` `/rotate` | daemon ctl POST `:8799` | DANGLING (502 ctl unreachable) | fleet buttons (3497/3511/3520/3483) |
| `/api/ctl/kill` | ctl POST, fallback writes `grid/KILL` directly | DANGLING ctl; LIVE local fallback (no-op without brain) | fleet "Halt" (3533) |
| `/api/ctl/unkill` | removes `grid/KILL` | LIVE (local file op) | fleet (3547) |
| `/api/config` | rewrites `grid/config.yaml` via `yaml_edit` (whitelist) | LIVE | config save (3131) |
| `/api/llm` | rewrites `state/llm.env` (atomic, 0600, `.bak`) | LIVE | config → LLM save (3347) |
| `/api/llm/validate` | subprocess `grid/llm/provider.py --ping` | LIVE / EXTERNAL pings | config → LLM validate (3293) |
| `/api/daemon/stop` | `grid/dev stop` (detached) | LIVE (supervisor control) | fleet (3674) |
| `/api/daemon/start` | `grid/dev start` (detached; `live_paper` ignored — DRY-RUN only) | LIVE | fleet (3620) |
| `/api/daemon/restart` | `grid/dev restart` (detached) | LIVE | fleet (3649) |
| `/api/dev/reset` `/api/dev/reset-wt` `/api/dev/clean` | detached `grid/dev <action> --yes` → log `state/logs/dev.log` | LIVE + **destructive** (reset wipes the frozen WT-era state backup-included) | fleet danger buttons (3594) |

### Engine banner / frozen marking that already exists

- `server.py:_freshness()` (line 2126) attaches `{at, at_iso, age_s, path, kind,
  note}` to `/api/decisions`, `/api/reports`, `/api/reliability`, `/api/config`
  — note text already says "brain is closed… frozen snapshot".
- `app.js:freshnessBadge` (line 255) hardcodes the pivot epoch
  `Date.UTC(2026, 8, 14, 21, 42, 37)` (mirroring `engine.json.since`); banners
  render at `#dec-freshness`, `#rc-freshness`, `#rel-freshness`, `#cfg-freshness`,
  `#log-freshness`.
- Fleet header renders `engine.json` legacy banner (app.js 1219–1224) and the
  readiness strip appends "WT legacy closed <date>" (1469–1473).
- `/api/optimizer` returns `applicable: false` + reason (honest N/A pane).

## 2. Why run cards and decisions are stale

The autonomy brain that *wrote* these files was retired with the WunderTrading
engine. `grid/state/engine.json` (mtime 2026-09-14T21:42:37Z) records the
pivot: `"since": "2026-09-14T21:42:37Z"`, `legacy.closed_reason: "WT paper
engine retired for the freqtrade pivot…"`. Since then `./grid/dev start` runs
only the console + one dry-run freqtrade engine; nothing writes deliberation
state. Evidence (all mtimes UTC):

- `/Volumes/ExMac/code/grid/0/freqtrade/grid/state/decisions.jsonl` — last
  write **2026-09-14T21:33:58Z** (last record `d20260914-501` at
  `2026-09-14T21:33:58+00:00`). Serves `/api/decisions` + `/api/decisions/<id>`.
- `grid/state/reports/` — 1157 files; newest rescreen card
  `20260914T210046Z-rescreen.{json,md}` (**2026-09-14T21:00:46Z**); newest
  audit card `audit-20260906-*`. Serves `/api/reports`, `/api/reports/<stem>`,
  `/api/screen`, and the overview screen rail.
- `grid/state/reliability.json` — **2026-09-14T20:11:09Z** (pre-pivot → the
  Decisions/Reliability banners already fire `predatesPivot`).
- `grid/state/state.json` — **2026-09-14T21:44:08Z**: `active_bots` is empty,
  `journal` is a 200-event ring, `screen_cache.at` = 2026-09-14T21:07:31Z. All
  journal-derived endpoints (`/api/pnl` fallback, `/api/position-sweeps`,
  `/api/recommendations` fallback, `/api/optimizer/swap-log`) render this ring.
- `grid/state/logs/daemon-launchd.log` — 2026-09-14T21:42:42Z (final brain
  stdout), kept only as the Logs pane's legacy source.

By contrast the freqtrade fleet is genuinely live: `state/ft_fleet/ft-btc/freqtrade.log`
was written 2026-09-14T23:18:26Z, REST :8191 answers, `state/logs/console.log`
22:48:28Z.

## 3. Absolute paths & out-of-workspace references the console touches

In-workspace (absolute, derived from `GRID_HOME = /Volumes/ExMac/code/grid/0/freqtrade/grid`):

- `grid/state/state.json`, `decisions.jsonl`, `engine.json`, `reliability.json`,
  `reliability_archive.json`, `llm.env`, `reports/`, `logs/` (incl.
  `daemon-launchd.log`, `daemon.log` legacy path, `dev.log`),
  `state/ft_fleet/registry.json`, `state/ft_fleet/*/grid.json`,
  `state/ft_fleet/*/freqtrade.log`, `state/daemon.pid` (legacy, absent)
- `grid/config.yaml`, `grid/dev`, `grid/llm/provider.py`,
  `grid/.pocketbase/pb.env`, `grid/pbclient.py`, `grid/strategies/`
  (`GridStrategy.py|.json`, `grid_geometry.py`), `grid/KILL`
- `<repo>/.venv-ft/bin/freqtrade` (readiness probe; present)

Out-of-workspace / external:

- `http://127.0.0.1:8799` — daemon ctl plane (`/status`, `/observe`, all
  `/api/ctl/*` POSTs); **no listener** (brain retired)
- `http://127.0.0.1:8090` (`PB_URL`) — PocketBase REST + `/api/health`;
  binary/config present but **not running**
- `http://127.0.0.1:8765` (`TVCLI_BASE`) — tvcli `/fetch` for OHLCV; running,
  upstream data is TradingView (external)
- `http://127.0.0.1:8191` (+ per-instance ports from `registry.json`) —
  freqtrade REST `/api/v1/ping`; running
- LLM provider APIs (Cloudflare, NVIDIA, OpenRouter, Mistral) pinged by
  `grid/llm/provider.py` — external network
- **Dangling code reference**: `server.py:816-818` inserts
  `grid/execution/` on `sys.path` and does `import reliability_grid` — that
  module exists **only** in the companion repo
  `/Volumes/ExMac/code/tradingview/go/agents/grid-autonomy/execution/reliability_grid.py`,
  so `/api/reliability/archive` always returns empty here.
- **Data-borne absolute paths**: `state/ft_fleet/registry.json` `archived[].archived_to`
  points into the companion repo
  (`/Volumes/ExMac/code/tradingview/go/agents/grid-autonomy/state/ft_fleet/archive/…`)
  and is surfaced verbatim through `/api/meta` → `engine` / `/api/overview` → `ft_fleet.archived_tail`.
- Env vars: `GRID_STATE_DIR`, `GRID_DAEMON_PORT`, `PB_URL`, `TVCLI_SERVER`,
  `CONSOLE_PORT`, `GRID_BIND_HOST`, `WT_ACCOUNT_LABEL`, `/.dockerenv` check,
  launchd label `com.tvcli.grid-autonomy` (absent here, probed via `launchctl`).

## 4. Minimal-change proposal for labeling frozen panes

Server-side (single helper, no schema break):

1. Extend `_freshness()` (server.py:2126) to also return a boolean:
   `frozen = bool(at and at < PIVOT_EPOCH)` and `frozen_at = engine.since`.
   Load `PIVOT_EPOCH` once at boot from `state/engine.json`
   (`legacy.closed_at` or `since`), falling back to the current hardcoded
   2026-09-14T21:42:37Z — this removes the duplicated pivot constant from
   app.js:265 (`Date.UTC(2026, 8, 14, 21, 42, 37)`).
2. Attach `_freshness(...)` (or a slim `{"frozen": true, "frozen_at": ...}`)
   to the payloads that lack it today: `/api/state`, `/api/journal`,
   `/api/screen`, `/api/optimizer/swap-log`, `/api/position-sweeps`,
   `/api/decisions/<id>`, `/api/reports/<stem>` (use the card's own `at`), and
   `/api/pnl` / `/api/recommendations` **only when they return the frozen
   state.json fallback** (`"source": "state"` + `frozen: true`; PB-live reads
   stay unflagged). `/api/decisions`, `/api/reports`, `/api/reliability`
   already carry `freshness`.
3. Optionally have `_freshness()` name the source class
   (`"origin": "wt-brain" | "grid-dev" | "freqtrade-engine"`) so the UI can
   word the banner correctly per pane.

Client-side (app.js):

1. `freshnessBadge()` (line 255): prefer server-provided `frozen`/`frozen_at`
   over the local `pivotEpoch` computation; keep the hardcoded epoch only as
   fallback for older responses.
2. `renderFreshnessBanner()` stays as-is; call it in the render functions that
   receive the newly flagged payloads — optimizer (`loadOptimizer`, add slots
   `#opt-sweeps-freshness`, `#opt-swap-freshness`), PnL timeline
   (`loadPnlTimeline`, `#pnl-freshness`), decisions drill-down
   (`openDecisionFromSlot`/row detail), and run-card detail (`openRunCard`,
   reuse `#rc-freshness` for the card's own `at`).
3. For `/api/pnl` + `/api/recommendations` fallback mode, render the existing
   banner variant when `payload.frozen && payload.source === "state"`
   ("PB unavailable — showing the frozen WT-era journal ring, last write
   <at_iso>").
4. Leave config.yaml exempt (already handled: `isConfig` skip in
   `freshnessBadge`), and leave the fleet engine banner (reads `engine.json.legacy`)
   unchanged — it is already correct.

No endpoint shapes are renamed; every change is additive fields + banner calls.
