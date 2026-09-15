# grid fleet console — observe · configure · maintain

The mission console for the **standalone freqtrade dry-run grid fleet**: a
stdlib-only backend (`server.py`) that reads workspace artifacts (the
fleet's trades DBs, the engines' own local REST APIs, `state/*` snapshots)
plus a vanilla JS/HTML/CSS frontend (`static/`). The WT-era autonomy brain
was retired with the WunderTrading engine (2026-09-15) — the console now
serves live freqtrade-fleet truth end to end and is purely additive and
fail-soft.

```
browser ── http://127.0.0.1:8798 ──> console/server.py
                                      ├─ reads   state/ft_fleet/<bot>/tradesv3.dryrun.sqlite
                                      │          (sqlite, read-only) + registry.json,
                                      │          state/engine.json, state/llm.env
                                      ├─ probes  each freqtrade engine's local REST
                                      │          (:8191+, HTTP Basic, 30s TTL + per-endpoint
                                      │          60s back-off) for marks/balance/candles
                                      ├─ edits   config.yaml (whitelist, comments preserved)
                                      └─ controls lifecycle via grid/dev (console + engines)
```

The retired daemon ctl plane (`:8799`) is **not** proxied anymore
(`CTL_RETIRED = True` in `server.py`): every former ctl consumer builds
its payload from workspace artifacts instead. `/api/observe` and
`/api/status` degrade fail-soft.

## Run

```sh
grid/dev start                  # whole stack: console + PB (:8290) + 4 dry-run engines
grid/dev stop --keep-ft --keep-pb   # stop JUST the console (engines keep trading)
grid/dev start --no-ft          # console-only restart — required after changing
                                # console/server.py (statics are served from disk
                                # per request, so frontend edits live on refresh;
                                # server.py edits need the process restart)
python3 console/server.py       # standalone: http://127.0.0.1:8798
GRID_BIND_HOST=0.0.0.0 …        # container override; local default stays loopback-only
```

Stdlib only — no pip installs, no build step. Binds `127.0.0.1` only.
Live values (status chips, marks, feed) update every 5s while the page is
visible; the LLM provider ping and the reliability ledger are 60s-cached
server-side (M4 pairing is not free).

## The UI

| View | What it shows / does |
|------|----------------------|
| **Fleet** | One live card per freqtrade instance: registry identity, engine-REST marks, trades-DB counts/realized, and the grid channel recomputed exactly the way GridStrategy does it (vendored geometry + tuned params + last analyzed candle — never persisted by the strategy, so nothing on disk is trusted for it). Status is three-state (running / starting / down, with an `api_backoff` tooltip while REST is in a 429/warm-up back-off). Inline 5m sparklines (tvcli/Binance market data; keyboard-openable market modal with channel refs), an honest "no market feed" note for symbols with no TV pair (HYPE). Right rail: lifecycle controls (Restart / Stop mission; Clear KILL only when the legacy artifact exists), live engine-event feed, LLM provider brains, fleet summary. A readiness strip on top probes engines, `.venv-ft`, strategy files, PocketBase (legacy, unused) and LLM env keys. |
| **Decisions** | The live engine decision stream — grid-line fills, trade opens and closes, derived per instance from `tradesv3.dryrun.sqlite`. Text filter. The WT-era `state/decisions.jsonl` ledger is preserved on disk but no longer rendered (no writer since the brain retired). |
| **Run cards** | The live engine session (per-instance status, trades, marks, channel, wallet). The WT-era `state/reports/` archive is preserved on disk and no longer displayed. |
| **Optimizer** | Not applicable in this workspace (GridStrategy revalues the channel in-strategy every candle) — the panel shows the deployed live geometry, the M2-hyperopt tuned params, and the reliability ledger **with an explicit provenance badge** ("live · trades DBs" vs "frozen WT-era file" when the live ledger is empty). |
| **Reliability** | Live M4 ledger computed from the fleet's trades DBs (pairing → ledger math), sizing-ladder thresholds pinned at top, per-archetype expandable round-trips; the frozen WT-era `state/reliability.json` snapshot renders as a clearly-labeled secondary card while the file exists. Auto-refreshes every 30s. |
| **Config** | Whitelisted knobs (portfolio, cadence, policy, sizing ladder, optimizer) edited in place — comments preserved, rolling `.bak`, round-trip verified. **Honest by design:** nothing in the standalone stack reads `config.yaml` at runtime (fleet params come from `grid/dev` constants + `GridStrategy.json`; ladder thresholds are constants in `grid/reliability/ledger.py`) — edits persist for the future autonomy brain (M3), and no restart "applies" them. LLM provider keys/models/chain/role-routing persist to `state/llm.env` (0600; keys never echo). |
| **Logs** | Workspace-tail: every live log file under `state/logs/` merged, grep + follow, append-only diffing with highlighted new lines. |

## API

Everything the UI does is a plain JSON endpoint (safe to curl):

| Method | Path | Effect |
|--------|------|--------|
| GET | `/api/overview` | Merged snapshot: daemon (grid/dev supervision of the console), engine declaration, live ft_fleet instances (enriched), readiness, journal tail (live engine events), reliability, config digest, PB health. |
| GET | `/api/daemon` | Supervisor/lifecycle detail (pid, mode, kill-file, uptime). |
| GET | `/api/state` · `/api/journal` | Raw `state.json` / journal tail (WT-era artifacts, fail-soft). |
| GET | `/api/decisions?limit=` | Frozen WT-era decisions + `engine_events` (live fills/opens/closes) + freshness. |
| GET | `/api/reliability` · `/api/reliability/archive?limit=` | Live M4 ledger (ladder, kill thresholds, freshness, WT-era file snapshot) / per-archetype closed round-trips. |
| GET | `/api/optimizer` | `{optimizer: null, applicable: false, reason, fast}` — `fast` carries the live geometry, tuned params, reliability + `reliability_source`, and the frozen decisions tail. |
| GET | `/api/llm/health` · `/api/llm` | Live provider pings + role routing (60s cache) / sidecar state for the editor. |
| GET | `/api/chart?venue=&symbol=&interval=&bars=` | tvcli/Binance candle proxy (`1m|3m|5m|15m|1h|4h|1d`; errors cached 60s). |
| GET | `/api/pnl` | Fleet-cumulative PnL timeline from the trades DBs + a trailing live mark point. |
| GET | `/api/reports` | Live engine session + WT-era report index (frozen, freshness-labeled). |
| GET | `/api/logs?lines=&grep=` | Workspace log-tail (per-source mtimes included). |
| GET | `/api/config` | Parsed `config.yaml` + editable whitelist + freshness. |
| GET | `/api/meta` | Ports, paths, engine declaration. |
| POST | `/api/config` `{"edits": {path: value}}` | Apply whitelisted edits (backup + round-trip check). |
| POST | `/api/llm` · `/api/llm/validate` | Persist `state/llm.env` / live-validate all providers. |
| POST | `/api/ctl/kill` `{confirm}` | Write the legacy `grid/KILL` file — **kept for API compat only; nothing in the standalone stack consumes it** (the WT-era brain did). The UI surfaces it only as a clearable legacy artifact. |
| POST | `/api/ctl/unkill` `{confirm}` | Remove the legacy KILL file. |
| POST | `/api/daemon/stop` `{confirm, force}` | `grid/dev stop` (detached so the response flushes; `force` also SIGKILLs the console). |
| POST | `/api/daemon/start` `{confirm, live_paper, clear_kill}` | `grid/dev start` (`live_paper` accepted for compat, ignored — dry-run only). |
| POST | `/api/daemon/restart` `{confirm, clear_kill, live_paper}` | `grid/dev` stop+start in the background. |
| POST | `/api/dev/reset` · `/api/dev/reset-wt` · `/api/dev/clean` `{confirm, …}` | Run the single `dev` script detached (confirm-gated; output in `state/logs/dev.log`; `reset`/`reset-wt` stop the console itself, so the frontend reloads after a few seconds). |

### Safety model

- **127.0.0.1 only**; cross-origin POSTs are refused.
- Destructive calls (`stop`, `restart`, `reset*`, `clean`) require
  `{"confirm": true}` — the UI backs these with explicit confirm dialogs.
- Config edits are restricted to a **whitelisted, range-checked** set of
  numeric/bool knobs; `autonomy.live_profiles` / `paper_profiles` are
  deliberately not editable from the console. Every write is
  comment-preserving (`yaml_edit.py`), round-trip verified against the
  YAML parser, and leaves `config.yaml.bak`.
- LLM keys are read from `state/llm.env` (0600) and only ever surface as
  presence booleans / masked inputs — never in any payload or log.
- Engine REST credentials live only in `state/ft_fleet/registry.json` +
  each instance's `config.json`; the console uses them for the
  Authorization header only and never returns them.
- The console never talks to any exchange directly; market data comes
  through the tvcli proxy and trade state through the local engines and
  their sqlite artifacts. Everything here is dry-run.

## Files

| Path | Role |
|------|------|
| `server.py` | HTTP backend: static serving, live-fleet APIs, chart proxy, config editor, grid/dev lifecycle ops. |
| `yaml_edit.py` | Path-aware, comment-preserving YAML leaf editor (block + one flow level). |
| `static/index.html` · `static/app.js` · `static/styles.css` | The frontend — vanilla, no dependencies, no build step. |
| `test_upgrade.py` | Offline unit + HTTP tests (isolated tmp state dir; never touches the real grid/KILL or fleet DBs). |

## Tests

```sh
python3 console/test_upgrade.py          # from grid/ (or any absolute path) — 14 tests
```

See `../docs/console-review-fixes-2026-09-15.md` for the recorded review
passes (misalignments found + fixed, verified live).
