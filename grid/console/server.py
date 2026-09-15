#!/usr/bin/env python3
"""console — observation + configuration + dev-control backend for the
standalone grid fleet.

A separate, additive HTTP service: it reads the workspace state artifacts
(state.json, decisions.jsonl, reliability.json, reports/), serves the live
freqtrade fleet (registry + engine REST + trades DBs), and adds the
operations the ctl plane deliberately lacked — whitelisted config.yaml edits
(comment-preserving), KILL-file management, and stack lifecycle control via
grid/dev. Serves the static frontend from ./static. The WT-era daemon ctl
plane (:8799) is RETIRED in this workspace — see CTL_RETIRED below.

Bindings: 127.0.0.1 only. Destructive calls require {"confirm": true}.
Stdlib only, like the rest of grid-autonomy.

Run:
    python3 console/server.py            # :8798
    CONSOLE_PORT=8800 python3 console/server.py

API (all JSON):
    GET  /api/overview        merged snapshot (daemon, ctl, state, bots,
                              reliability, screen, config digest, PB health)
    GET  /api/daemon          supervisor/lifecycle detail
    GET  /api/state           raw state.json
    GET  /api/journal?limit=  journal tail (newest last, as stored)
    GET  /api/decisions?limit=decisions.jsonl tail (newest first)
    GET  /api/decisions/<id>   one decision + cohort (same symbol+regime)
    GET  /api/reliability     archetype ledger + sizing-tier computation
    GET  /api/recommendations?limit=  position-optimizer recommendations
                              (PocketBase records, newest first)
    GET  /api/screen          latest rescreen run card extract
    GET  /api/optimizer        standalone payload — the WT-era slow-loop
                              optimizer has no freqtrade counterpart
                              (applicable:false + live geometry, tuned
                              params, M4 ledger, decision tail)
    GET  /api/optimizer/swap-log  swap_log + per-slot idle trackers +
                              last arbiter verdict from state.optimizer
                              (fail-soft empty)
    GET  /api/reports         run-card index + live engine session
    GET  /api/reports/<stem>  one run card {json, md}
    GET  /api/logs?lines=&grep=  log tail — workspace-tail merges every
                              live log (state/logs/* + per-engine logs)
    GET  /api/config          parsed config.yaml + editable whitelist
    GET  /api/observe         daemon ctl proxy — RETIRED (fail-soft error)
    GET  /api/status          daemon ctl proxy — RETIRED (fail-soft error)
    GET  /api/pnl             PnL history — post-pivot PocketBase journal
                              rows when present, else one merged fleet
                              timeline from the dry-run trades DBs
                              (cumulative realized + live open-position
                              mark)
                              → {points: [{at, fleet{…}}]} newest-first
    GET  /api/chart?venue=&symbol=&interval=&bars=
                              OHLCV window for a slot's market — proxy of
                              the tvcli server's POST /fetch (interval ∈
                              1m|3m|5m|15m|1h|4h|1d, bars 8..500, 60s
                              in-process cache; fail-soft:
                              {"error": …, "bars": []} + 200 when tvcli
                              is down)
                              → {at, venue, symbol, interval,
                                 bars: [{t, o, h, l, c}] oldest-first}
    GET  /api/position-sweeps position-optimizer sweep history — journal
                              ring entries of kind position-optimizer-sweep /
                              position-optimizer / position-optimizer-applied
                              from state.json (fail-soft when absent)
                              → {sweeps: [{…}]} newest-first, last 25
    GET  /api/meta            ports, paths, versions
    GET  /api/llm/health      live provider ping + role routing matrix
                              (60s in-process cache; keys never returned)
    POST /api/ctl/rescreen    RETIRED — the standalone workspace has no
    POST /api/ctl/optimize    daemon ctl plane; these return 502 (the
    POST /api/ctl/reliability freqtrade engine + GridStrategy own the
    POST /api/ctl/rotate      loop, nothing to queue into)
    POST /api/ctl/kill        write the KILL file            {confirm}
    POST /api/ctl/unkill      remove the KILL file           {confirm}
    POST /api/config          apply whitelisted edits {edits:{path:value}}
    POST /api/daemon/stop     stop the stack via grid/dev   {confirm, force}
    POST /api/daemon/start    grid/dev start [--no-ft]       {confirm,
                              live_paper (ignored), clear_kill}
    POST /api/daemon/restart  grid/dev stop+start            {confirm,
                              clear_kill, live_paper (ignored)}
    POST /api/dev/reset       run `dev reset` (detached; wipes runtime
                              state, stops the stack; --keep-decisions /
                              --wt / --start)   {confirm, keep_decisions,
                              wt, start}
    POST /api/dev/reset-wt    run `dev reset-wt` (detached; deletes all
                              WunderTrading PAPER grid bots) {confirm}
    POST /api/dev/clean       run `dev clean` (detached; clears logs +
                              runtime artifacts)            {confirm}
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
GRID_HOME = os.path.dirname(HERE)
sys.path.insert(0, GRID_HOME)          # config_lite
sys.path.insert(0, HERE)               # yaml_edit

import yaml_edit  # noqa: E402
from config_lite import load_yaml  # noqa: E402

CONSOLE_PORT = int(os.environ.get("CONSOLE_PORT", "8798"))
STATE_DIR = os.environ.get("GRID_STATE_DIR") or os.path.join(GRID_HOME, "state")
CONFIG_PATH = os.path.join(GRID_HOME, "config.yaml")
STATIC_DIR = os.path.join(HERE, "static")
KILL_FILE = os.path.join(GRID_HOME, "KILL")
LAUNCHD_LABEL = "com.tvcli.grid-autonomy"
# The launchd-supervised daemon's stdout/stderr go here (see
# launchd/com.tvcli.grid-autonomy.plist), NOT state/daemon.log — start.sh
# writes daemon.log only for manual/nohup launches. The console must read
# the right file depending on the supervisor, or the Logs view silently
# shows a stale/empty file in the normal (supervised) production case.
LAUNCHD_LOG = os.path.join(STATE_DIR, "logs", "daemon-launchd.log")
PB_URL = os.environ.get("PB_URL", "http://127.0.0.1:8090").rstrip("/")

# Which execution engine the fleet runs on — state/engine.json, written by
# ops when the active engine changes (WunderTrading paper → freqtrade).
# {"engine", "mode", "since", "note", "legacy": {...}}; fail-soft → None.
ENGINE_PATH = os.path.join(STATE_DIR, "engine.json")

# WT account label surfaced in the UI header + fleet summary. The VPS
# container uses the vault item; the Mac uses its own browser session.
WT_ACCOUNT_LABEL = os.environ.get("WT_ACCOUNT_LABEL") or (
    "vps (vault account)" if os.path.isfile("/.dockerenv") else "local (Mac account)")

# LLM provider sidecar (set/choose/validate from the console). Mirrors the
# .pocketbase/pb.env "export KEY=\"val\"" format; the daemon sources it via
# run_launchd.load_llm_env / start.sh / self_heal_env. Keys land here chmod
# 0600 and are NEVER returned by any /api/llm endpoint (presence boolean only).
LLM_ENV_PATH = os.path.join(STATE_DIR, "llm.env")

# Provider order + role keys, mirrored from llm/provider.py so the API can
# report them without importing the daemon module (kept in sync manually).
LLM_PROVIDERS = ["cf", "nvidia", "openrouter", "mistral"]
LLM_ROLE_KEYS = ["bull", "bear", "bull_rebuttal", "bear_rebuttal", "facilitator",
                 "risk_seeking", "risk_neutral", "risk_conservative"]

DEFAULT_CTL_PORT = 8799

# Sizing-ladder thresholds (mirror daemon.size_multiplier semantics; surfaced
# so the UI can tier archetypes without hardcoding them client-side).
LADDER = {"probe_samples": 10, "full_samples": 30, "pf_pass": 1.3, "pf_kill": 1.0}

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
        ".png": "image/png", ".ico": "image/x-icon", ".json": "application/json"}

# ── editable config whitelist (path -> rule) ───────────────────────────
# Safety-critical lists (autonomy.live_profiles, paper_profiles) are
# deliberately NOT editable through the console.
EDITABLE = {
    "portfolio.total_usd": dict(t="float", min=10, max=1_000_000, group="Portfolio",
                                label="Fund size", unit="USD"),
    "portfolio.slots_default": dict(t="int", min=1, max=10, group="Portfolio",
                                    label="Slots"),
    "portfolio.slots_max": dict(t="int", min=3, max=10, group="Portfolio",
                                label="Max slots (fixed venues)"),
    "portfolio.slots_hard_max": dict(t="int", min=3, max=64,
                                     group="Portfolio",
                                     label="Hard slot ceiling (dynamic venues)"),
    "portfolio.min_slot_usd": dict(t="float", min=20, max=10_000,
                                    group="Portfolio",
                                    label="Min slot budget", unit="USD"),
    "portfolio.max_alloc_per_slot": dict(t="float", min=0.05, max=1.0,
                                         group="Portfolio",
                                         label="Max allocation per slot",
                                         unit="fraction"),
    "portfolio.cash_buffer_pct": dict(t="float", min=0.0, max=0.9,
                                      group="Portfolio", label="Cash buffer",
                                      unit="fraction"),
    "portfolio.venues.hyperliquid.balance_usd": dict(
        t="float", min=0, max=1_000_000, group="Portfolio",
        label="Hyperliquid sleeve", unit="USD"),
    "portfolio.venues.binance.balance_usd": dict(
        t="float", min=0, max=1_000_000, group="Portfolio",
        label="Binance sleeve", unit="USD"),
    "screen.rescreen_minutes": dict(t="float", min=5, max=1440, group="Cadence",
                                    label="Rescreen cadence", unit="min"),
    "grid_defaults.take_profit_pct": dict(
        t="float", min=0, max=2, group="Exits",
        label="Profit-exit target", unit="× slot budget",
        help="Cumulative total PnL (realized + mark) at which a bot is "
             "stopped at profit and its slot recycled. 0 disables. "
             "Never closes a losing line."),
    "screen.min_volume_usd": dict(t="int", min=100_000, max=100_000_000,
                                  group="Screening",
                                  label="Min 24h quote volume", unit="USD"),
    "screen.universe_max_symbols": dict(t="int", min=10, max=300,
                                        group="Screening",
                                        label="Universe size per venue",
                                        unit="symbols"),
    "screen.open_slot_min_score": dict(t="float", min=0, max=200,
                                       group="Screening",
                                       label="New-slot score floor",
                                       unit="pts"),
    "watch.interval_s": dict(t="float", min=10, max=3600, group="Cadence",
                             label="Health poll", unit="s"),
    "watch.adjust_steps_threshold": dict(t="float", min=0.5, max=10,
                                         group="Cadence",
                                         label="Re-centre drift", unit="steps"),
    "watch.gone_warn_after": dict(t="int", min=1, max=30, group="Cadence",
                                  label="Gone-bot warn after",
                                  unit="ticks"),
    "watch.gone_clear_min": dict(t="float", min=1, max=720, group="Cadence",
                                  label="Gone-bot slot clear", unit="min"),
    "policy.hysteresis_score": dict(t="float", min=0, max=50, group="Policy",
                                    label="Rotation hysteresis", unit="pts"),
    "policy.min_hold_h": dict(t="float", min=0, max=720, group="Policy",
                              label="Min hold", unit="h"),
    "autonomy.base_pct": dict(t="float", min=0.01, max=1.0, group="Sizing ladder",
                              label="Base tier", unit="fraction"),
    "autonomy.probe_pct": dict(t="float", min=0.01, max=1.0, group="Sizing ladder",
                               label="Probe tier", unit="fraction"),
    "autonomy.full_pct": dict(t="float", min=0.01, max=1.0, group="Sizing ladder",
                              label="Full tier", unit="fraction"),
    "autonomy.tier_max_grids.base": dict(
        t="int", min=3, max=100, group="Sizing ladder",
        label="Base-tier grid cap", unit="lines"),
    "autonomy.tier_max_grids.probe": dict(
        t="int", min=3, max=100, group="Sizing ladder",
        label="Probe-tier grid cap", unit="lines"),
    "autonomy.tier_max_grids.full": dict(
        t="int", min=3, max=100, group="Sizing ladder",
        label="Full-tier grid cap", unit="lines"),
    "reliability.kill_min_samples": dict(
        t="int", min=1, max=100, group="Sizing ladder",
        label="Kill-flag min samples", unit="trips"),
    "memory.k": dict(t="int", min=1, max=10, group="Deliberation",
                     label="Memories per candidate"),
    "optimizer.enabled": dict(t="bool", group="Optimizer",
                              label="Fast loop enabled"),
    "optimizer.interval_min": dict(t="float", min=2, max=5, group="Optimizer",
                                   label="Hunt cadence", unit="min"),
    "optimizer.idle_minutes": dict(t="float", min=1, max=120,
                                   group="Optimizer",
                                   label="Idle floor", unit="min"),
    "optimizer.idle_k": dict(t="float", min=0.25, max=5, group="Optimizer",
                             label="Idle × expected interval"),
    "optimizer.min_hold_min": dict(t="float", min=0, max=240,
                                   group="Optimizer",
                                   label="Fast min-hold", unit="min"),
    "optimizer.upgrade_margin": dict(t="float", min=1, max=50,
                                     group="Optimizer",
                                     label="Upgrade margin", unit="pts"),
    "optimizer.arbiter_margin": dict(t="float", min=0, max=50,
                                     group="Optimizer",
                                     label="Arbiter margin", unit="pts"),
    "optimizer.min_swap_interval_min": dict(t="float", min=5, max=720,
                                            group="Optimizer",
                                            label="Swap rate limit / slot",
                                            unit="min"),
    "optimizer.max_swaps_per_hour": dict(t="int", min=1, max=20,
                                         group="Optimizer",
                                         label="Swaps / hour cap"),
    "optimizer.hunt_top": dict(t="int", min=1, max=20, group="Optimizer",
                               label="Challengers refreshed"),
    "optimizer.fail_cooldown_min": dict(t="int", min=5, max=720,
                                        group="Optimizer",
                                        label="Failed-challenger cooldown",
                                        unit="min"),
    "optimizer.screen_cache_fresh_min": dict(t="float", min=10, max=1440,
                                             group="Optimizer",
                                             label="Cache freshness",
                                             unit="min"),
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── small readers (never raise) ────────────────────────────────────────

def _read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _tail(path, max_bytes=512 * 1024):
    """Last chunk of a file as text (files here are modest; bounded read)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _load_state():
    st = _read_json(os.path.join(STATE_DIR, "state.json"), {}) or {}
    st.setdefault("slots", [])
    st.setdefault("active_bots", {})
    st.setdefault("committed", {})
    st.setdefault("journal", [])
    st.setdefault("cooldowns_until", {})
    return st


def _ctl_port() -> int:
    port = os.environ.get("GRID_DAEMON_PORT")
    if port:
        try:
            return int(port)
        except ValueError:
            pass
    cfg = load_yaml(open(CONFIG_PATH).read()) if os.path.isfile(CONFIG_PATH) else {}
    try:
        return int((cfg.get("server") or {}).get("daemon_port", DEFAULT_CTL_PORT))
    except Exception:
        return DEFAULT_CTL_PORT


def _http_json(url, timeout=3.0, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return False, json.loads(exc.read() or b"{}")
        except Exception:
            return False, {"error": f"HTTP {exc.code}"}
    except Exception as exc:
        # transport failure (connection refused, timeout): a plain string,
        # NOT an {"error": …} dict — that shape is reserved for a body the
        # daemon actually answered with, and laundering exceptions into it
        # made _ctl_err surface raw urlopen text instead of "ctl unreachable"
        return False, str(exc)[:200]


# ── daemon ctl plane (:8799) — RETIRED in this workspace ────────────────
# The WT-era brain this proxy used to drive was closed at the 2026-09-14
# pivot (state/engine.json → legacy.closed_at), and nothing in the
# standalone stack serves :8799. The M3 companion repo's daemon DOES
# (different workspace, cwd .../grid-autonomy) and it answers with the
# legacy /status shape — so before this flag existed the console silently
# fused to a FOREIGN system: /api/overview's readiness probe rendered the
# companion daemon's diagnostics as if they were ours, and POST
# /api/ctl/kill would have armed THEIR halt instead of writing the local
# KILL file. Every ctl call is therefore hard-disabled; the console stays
# workspace-contained. Flip this only if a ctl plane ever ships INSIDE
# grid/dev with its own port.
CTL_RETIRED = True


def _ctl(path, method="GET", body=None):
    if CTL_RETIRED:
        return False, {"error": "ctl retired — the standalone workspace "
                                "has no daemon ctl plane (:8799 belongs "
                                "to the M3 companion repo's daemon and is "
                                "never read or driven from here)"}
    return _http_json(f"http://127.0.0.1:{_ctl_port()}{path}", 3.0, method, body)


def _ctl_err(resp):
    """Error copy for a failed ctl call: the daemon's own message when it
    ANSWERED with a JSON error body (e.g. POST /optimize 503 "optimizer
    unavailable (import failed)"), "ctl unreachable" only when nothing
    answered. Masking daemon errors as unreachable sends the operator
    debugging the connection instead of the daemon."""
    return (resp.get("error") if isinstance(resp, dict) else None) \
        or "ctl unreachable"


# ── ctl-plane proxy cache (≤5s: several panels share one /status) ──────

_CTL_TTL = 5.0
_CTL_CACHE: dict = {}


def _ctl_cached(path):
    """GET a ctl resource with a ≤5s cache so parallel console panels
    (overview, fleet header, status proxy) reuse one daemon round-trip."""
    now = time.time()
    hit = _CTL_CACHE.get(path)
    if hit and hit[0] > now:
        return hit[1], hit[2]
    ok, body = _ctl(path)
    _CTL_CACHE[path] = (now + _CTL_TTL, ok, body)
    return ok, body


# ── PocketBase side-channel access (journal `pnl-snapshot` history) ────

PB_ENV_PATH = os.path.join(GRID_HOME, ".pocketbase", "pb.env")


def _pb_env() -> dict:
    """Parse the local PB sidecar env file into {KEY: value} ({} if absent).

    Values are used for Authorization only and are never returned by any
    console endpoint."""
    env = {}
    try:
        with open(PB_ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.removeprefix("export ").partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key:
                    env[key] = val
    except OSError:
        pass
    return env


_PB_CLIENT = None


def _pb_client():
    """Lazily-built shared pbclient.PB() for journal reads (never raises).

    Seeds os.environ with the PB_* keys from the local .pocketbase/pb.env
    sidecar — ONLY keys not already present — so PB() picks up the
    superuser credentials and transparently re-auths when the stored
    PB_TOKEN JWT is stale (the journal collection blocks public reads).
    Returns None on any failure; callers fall through to the raw HTTP /
    state.json paths. Credentials are used for Authorization only and are
    never logged or returned by any console endpoint."""
    global _PB_CLIENT
    if _PB_CLIENT is not None:
        return _PB_CLIENT
    try:
        env = _pb_env()
        for key, val in env.items():
            if key.startswith("PB_") and os.environ.get(key) is None:
                os.environ[key] = val
        import pbclient  # GRID_HOME is already on sys.path
        _PB_CLIENT = pbclient.PB(url=(os.environ.get("PB_URL") or PB_URL))
    except Exception:
        return None
    return _PB_CLIENT


def _pb_get(url, timeout=2.5, token=None):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, json.loads(resp.read() or b"{}")
    except Exception as exc:
        return False, {"error": str(exc)[:200]}


def _pnl_points(items) -> list:
    """Normalize journal records/events of kind pnl-snapshot into
    [{at, fleet}] — tolerant of the payload living at the top level, in
    `extra` (the PB journal collection's free field), or being absent
    entirely (pre-restart daemon)."""
    pts = []
    for r in items or []:
        if not isinstance(r, dict):
            continue
        fleet = r.get("fleet")
        bots = r.get("bots")
        extra = r.get("extra")
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except Exception:
                extra = None
        if not isinstance(fleet, dict) and isinstance(extra, dict):
            fleet = extra.get("fleet")
            bots = bots or extra.get("bots")
        pts.append({"at": r.get("at"),
                    "fleet": fleet if isinstance(fleet, dict) else None,
                    "bots": bots if isinstance(bots, dict) else None})
    pts.sort(key=lambda p: p.get("at") or "", reverse=True)
    return pts[:200]


def _after_pivot(at) -> bool:
    """True when an ISO timestamp postdates the engine pivot
    (state/engine.json since). Pre-pivot timestamps are WT-era history
    and must never render as the current mission."""
    piv = (engine_payload() or {}).get("since") or "2026-09-14T21:42:37Z"

    def _ep(s):
        try:
            return datetime.fromisoformat(
                str(s).replace("Z", "+00:00")).timestamp()
        except Exception:
            return None
    a, p = _ep(at), _ep(piv)
    if a is None or p is None:
        return str(at or "") >= str(piv)
    return a >= p


def pnl_payload() -> dict:
    """PnL history for the timeline — live, workspace-contained end to end.

    Source order:
    1. The workspace PocketBase journal (kind='pnl-snapshot') — grid/dev
       serves the sidecar on :8290 (pb.env + the PB_URL env it exports).
       ONLY POST-PIVOT rows count: the vendored pb_data is the WT-era
       journal archive and its pre-pivot snapshots are history, never the
       current mission. (Nothing writes post-pivot rows yet — this branch
       is the forward-compat path for the journaler that comes with the
       brain.)
    2. The freqtrade fleet's dry-run trades DBs: one point per closed
       trade (cumulative realized, at=close_date) plus a trailing 'now'
       point carrying the open position's live mark (engine REST
       /status). The old frozen state.json journal fallback is gone for
       good — it could only ever serve pre-pivot snapshots."""
    # 1) post-pivot PB journal rows
    try:
        pb = _pb_client()
        records = None
        if pb is not None:
            try:
                records = pb.list("journal", filter="(kind='pnl-snapshot')",
                                  sort="-at", per_page=200)
            except Exception:
                records = None
        if not records:
            flt = urllib.parse.quote("(kind='pnl-snapshot')")
            url = (f"{PB_URL}/api/collections/journal/records"
                   f"?perPage=200&sort=-at&filter={flt}")
            ok, body = _pb_get(url)
            items = (body or {}).get("items") if ok and isinstance(body, dict) else None
            if not items:
                token = _pb_env().get("PB_TOKEN")
                if token:
                    ok, body = _pb_get(url, token=token)
                    items = (body or {}).get("items") \
                        if ok and isinstance(body, dict) else None
            records = items or []
        pts = [p for p in _pnl_points(records)
               if _after_pivot(p.get("at"))]
        if pts:
            return {"points": pts, "source": "pocketbase",
                    "total": len(pts),
                    "note": "post-pivot pnl-snapshot journal rows "
                            "(workspace PocketBase :8290)"}
    except Exception:
        pass
    # 2) live per-instance dry-run trades DBs + engine REST marks.
    # ONE merged fleet timeline: every closed trade (from any instance)
    # advances the fleet-wide cumulative realized, and a single trailing
    # live point carries the fleet's realized total plus the sum of the
    # open-position marks. The previous shape emitted one cumulative
    # series PER BOT all under the same `fleet` key — the frontend chart
    # plotted them as one line and sawtoothed between bots' own sums.
    events = []          # (close_date, profit, bot_code) — raw db strings
    fleet_unrealized = 0.0
    fleet_open_realized = 0.0
    has_live_mark = False
    for code, entry in (_ft_registry().get("instances") or {}).items():
        if not isinstance(entry, dict):
            continue
        db = os.path.join(entry.get("dir") or "", "tradesv3.dryrun.sqlite")
        if os.path.isfile(db):
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                try:
                    cur = con.cursor()
                    cur.execute("SELECT close_date, close_profit_abs FROM trades "
                                "WHERE is_open=0 AND close_date IS NOT NULL "
                                "ORDER BY close_date")
                    events.extend(
                        (at, float(profit or 0.0), code)
                        for at, profit in cur.fetchall())
                    # realized banked inside still-open grid trades
                    # (partial exits) — realized_profit is the cumulative
                    cur.execute("SELECT coalesce(sum(realized_profit), 0) "
                                "FROM trades WHERE is_open=1")
                    fleet_open_realized += \
                        float(cur.fetchone()[0] or 0.0)
                finally:
                    con.close()
            except Exception:
                pass
        stats = _engine_instance_stats(entry)
        if stats["api_ok"] and stats["open_trades"]:
            fleet_unrealized += stats["open_profit_abs"] or 0.0
            has_live_mark = True
    events.sort(key=lambda e: e[0] or "")
    points = []
    realized = 0.0
    for at, profit, code in events[-199:]:
        realized += profit
        points.append({
            "at": _iso_db_utc(at), "bot_code": code,
            "fleet": {"realized": round(realized, 6),
                      "unrealized": 0.0,
                      "net": round(realized, 6)}})
    if has_live_mark:
        realized_live = realized + fleet_open_realized
        points.append({
            "at": utcnow(), "bot_code": "fleet", "live": True,
            "fleet": {"realized": round(realized_live, 6),
                      "unrealized": round(fleet_unrealized, 6),
                      "net": round(realized_live + fleet_unrealized, 6)}})
    return {"points": list(reversed(points)),
            "source": "freqtrade-dryrun",
            "total": len(points),
            "note": "fleet-cumulative realized accrues per closed dry-run "
                    "trade (any instance) plus the partial-exit realized "
                    "still held inside open grid trades; the trailing live "
                    "point carries both plus the fleet's open-position "
                    "mark (engine REST). Written by nothing else — there "
                    "is no WT-era snapshot in this feed."}


# ── market OHLCV proxy (tvcli /fetch) for slot sparklines ──────────────

# Base URL of the tvcli serve daemon; read at import like PB_URL, and
# referenced through the module global so tests can point it at a stub.
TVCLI_BASE = os.environ.get("TVCLI_SERVER", "http://127.0.0.1:8765")
# CHART_INTERVALS — the dropdown set the mission console exposes to the
# frontend chart picker. Post-2026-09-15 reset: every slot runs in the
# 1m-5m band, so the lower-TF intervals are surfaced first and the
# 15m stays as the swing anchor the swarm uses as its highest-TF
# context bar. 1h / 4h / 1d remain available for context comparison.
CHART_INTERVALS = ("1m", "3m", "5m", "15m", "1h", "4h", "1d")
CHART_TTL = 60.0            # seconds a fetched window stays fresh
CHART_CACHE_MAX = 32        # bounded in-process cache (keys are 4-tuples)

_CHART_CACHE: dict = {}     # key -> (expiry_ts, payload)


def _tv_symbol(symbol: str) -> str:
    """TradingView symbol for a console venue/base pair (mirrors
    market_regime._tv_symbol): uppercase, no slash, USDT/USDC/BUSD quote
    kept, else USDT appended; BOTH venues ride the Binance USDT pair
    (hyperliquid perps have no TV feed of their own)."""
    s = (symbol or "").upper().replace("/", "")
    if not s.endswith(("USDT", "USDC", "BUSD")):
        s += "USDT"
    return f"BINANCE:{s}"


def _chart_bars(venue: str, symbol: str, interval: str, bars) -> tuple[int, dict]:
    """OHLCV window for the fleet sparklines, proxied from tvcli /fetch.

    Returns (status_code, payload): 400 for a bad venue/interval/symbol,
    otherwise 200. tvcli returns periods newest-first; we sort ascending
    and emit slim {t, o, h, l, c} bars. A 60s in-process cache (bounded
    to 32 keys, oldest-expiry evicted) keeps the 5s console poll from
    hammering tvcli. tvcli outages degrade to 200 + {"error": …,
    "bars": []} — fail-soft, like every other proxy here."""
    venue = (venue or "").strip().lower()
    if venue not in ("binance", "hyperliquid"):
        return 400, {"error": "unknown venue (binance|hyperliquid)"}
    interval = (interval or "1h").strip()
    if interval not in CHART_INTERVALS:
        return 400, {"error": f"bad interval ({'|'.join(CHART_INTERVALS)})"}
    symbol = (symbol or "").strip()
    if not symbol:
        return 400, {"error": "missing symbol"}
    try:
        n = int(bars)
    except (TypeError, ValueError):
        n = 96
    n = max(8, min(500, n))

    key = (venue, symbol, interval, n)
    now = time.time()
    hit = _CHART_CACHE.get(key)
    if hit and hit[0] > now:
        return 200, hit[1]

    ok, body = _http_json(f"{TVCLI_BASE}/fetch", 30.0, "POST",
                          {"symbol": _tv_symbol(symbol),
                           "timeframe": interval, "bars": n})
    periods = body.get("periods") if ok and isinstance(body, dict) else None
    base = {"at": utcnow(), "venue": venue, "symbol": symbol,
            "interval": interval}

    def _cache(payload):
        # Cache FAILURES on the same 60s TTL as successes: a symbol with no
        # upstream feed (e.g. HYPE has no Binance TV pair) must not re-hang
        # tvcli for the full 30s timeout on every 5s console poll.
        _CHART_CACHE[key] = (now + CHART_TTL, payload)
        while len(_CHART_CACHE) > CHART_CACHE_MAX:
            oldest = min(_CHART_CACHE, key=lambda k: _CHART_CACHE[k][0])
            del _CHART_CACHE[oldest]

    if not isinstance(periods, list):
        err = body.get("error") if isinstance(body, dict) else None
        payload = {**base, "bars": [], "count": 0,
                   "error": err or "tvcli unreachable"}
        _cache(payload)
        return 200, payload
    out = []
    for per in sorted(periods, key=lambda p: (p.get("time") or 0)
                      if isinstance(p, dict) else 0):
        try:
            out.append({"t": int(per["time"]), "o": float(per["open"]),
                        "h": float(per["high"]), "l": float(per["low"]),
                        "c": float(per["close"])})
        except (KeyError, TypeError, ValueError):
            continue
    payload = {**base, "bars": out, "count": len(out)}
    _cache(payload)
    return 200, payload


# ── daemon lifecycle helpers ───────────────────────────────────────────

def _pid() -> int | None:
    try:
        with open(os.path.join(STATE_DIR, "daemon.pid")) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def _ps(pid, field) -> str | None:
    try:
        out = subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


def _launchd_managed() -> bool:
    try:
        out = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
                             capture_output=True, timeout=5)
        return out.returncode == 0
    except Exception:
        return False


def _log_source() -> dict:
    """Resolve which file actually holds the daemon's stdout/stderr.

    The supervisor decides the sink: the launchd daemon redirects its stdio
    in-process to LAUNCHD_LOG (launchd itself cannot open files on this
    removable volume, so the plist sends early stdio to /dev/null), while
    start.sh/manual runs append to state/daemon.log. Prefer the supervised file when launchd manages the
    daemon (the normal production case) but fall back to the state log when
    the supervised file is absent/empty — e.g. a manual run while the agent
    is loaded-but-not-running, or a fresh install before first supervised
    boot. Report both the chosen path and which source is authoritative so
    the UI can label the view honestly instead of showing a silently wrong
    file.

    Standalone workspace (no launchd brain, no daemon.log): surfaces a
    virtual concatenated tail of every active log under state/logs/ plus
    each freqtrade instance's per-engine log. The UI gets a `sources`
    list so a user can pick which stream they want.
    """
    state_log = os.path.join(STATE_DIR, "daemon.log")
    managed = _launchd_managed()

    def _nonempty(path):
        try:
            return os.path.getsize(path) > 0
        except OSError:
            return False

    def _mtime(path):
        try:
            return os.stat(path).st_mtime
        except OSError:
            return None

    if managed and _nonempty(LAUNCHD_LOG):
        return {"path": LAUNCHD_LOG, "source": "launchd",
                "managed": True, "state_log": state_log}

    # Standalone-workspace fallback: the WT-era daemon.log is absent (no
    # brain). Gather every readable log under state/logs/ and per-instance
    # freqtrade logs into one virtual tail.
    sources = []
    log_dir = os.path.join(STATE_DIR, "logs")
    if os.path.isdir(log_dir):
        for name in sorted(os.listdir(log_dir)):
            p = os.path.join(log_dir, name)
            if os.path.isfile(p) and _nonempty(p):
                sources.append({"path": p, "label": name, "kind": "console",
                                "mtime": _mtime(p)})
    fleet_dir = os.path.join(STATE_DIR, "ft_fleet")
    if os.path.isdir(fleet_dir):
        for entry in sorted(os.listdir(fleet_dir)):
            p = os.path.join(fleet_dir, entry, "freqtrade.log")
            if os.path.isfile(p) and _nonempty(p):
                sources.append({"path": p, "label": f"{entry}/freqtrade",
                                "kind": "engine", "mtime": _mtime(p)})

    if not sources:
        return {"path": state_log, "source": "state",
                "managed": managed, "state_log": state_log,
                "sources": [],
                "note": "no logs found under state/logs/ or state/ft_fleet/*/ — "
                        "the WT-era daemon.log does not exist (brain is closed)"}

    # Concatenate all live log files newest-first within each, with a
    # small per-source header. Single source → just that file.
    def _tail_lines(p, n=600):
        try:
            with open(p, "rb") as f:
                data = f.read()[-1024 * 256:]
            return data.decode("utf-8", "replace").splitlines()[-n:]
        except OSError:
            return []

    blocks = []
    for s in sources:
        rows = _tail_lines(s["path"])
        if not rows:
            continue
        header = f"\n── {s['label']} ({s['path']}) ──"
        blocks.append(header + "\n" + "\n".join(rows))
    merged = "\n".join(blocks)
    return {"path": state_log, "source": "workspace-tail",
            "managed": managed, "state_log": state_log,
            "sources": sources,
            "lines_text": merged,
            "note": "Tailing all live log files (console + per-instance "
                    "freqtrade) since the WT-era daemon.log does not exist "
                    "— the brain is closed in this workspace."}


def _mode(pid) -> str:
    cmd = (_ps(pid, "command") or "") if pid else ""
    if "run_launchd.py" in cmd:
        return "live-paper"  # the launcher hardcodes --live-paper
    if "--live-paper" in cmd:
        return "live-paper"
    if cmd and "daemon.py" in cmd:
        return "dry-run"
    m = re.findall(r"dry_run=(True|False)", _tail(_log_source()["path"],
                                                  64 * 1024))
    if m:
        return "live-paper" if m[-1] == "False" else "dry-run"
    return "unknown"


def daemon_info() -> dict:
    """Supervisor/lifecycle detail for the #fleet status bar.

    In this checkout the supervisor is `grid/dev` (the WT-era launchd brain
    is gone with the WT engine), so we read truth from the :8798 listener
    and the `grid/dev status` output instead of the launchd ctl plane and
    the absent `daemon.pid`. The legacy fields are still returned so the UI
    doesn't have to special-case the standalone checkout.
    """
    pid = _dev_console_pid()
    info = {
        "running": pid is not None,
        "pid": pid,
        "supervisor": "grid/dev" if pid else "none",
        "mode": "dry-run",  # this workspace is DRY-RUN only
        "cmdline": _ps(pid, "command") if pid else None,
        "started_at": _ps(pid, "lstart") if pid else None,
        "kill_file": os.path.exists(KILL_FILE),
        # PocketBase sidecar health (grid/dev serves it on :8290 and exports
        # PB_URL into the console env). 8090 is the M3 stack's port and is
        # never probed from this workspace.
        "ctl_reachable": _http_json(f"{PB_URL}/api/health", 1.5)[0],
    }
    if pid:
        try:
            info["uptime_s"] = int(time.time() - ps_console_started_at(pid))
        except Exception:
            pass
    return info


def ps_console_started_at(pid) -> float:
    """Lsof LISTEN timestamp for the :8798 socket (OS-reported start)."""
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        # ps lstart is human-readable ("Mon Jan 1 12:34:56 2024") — parse
        # with asctime; on failure fall back to the file mtime of the script.
        from time import strptime, mktime
        return mktime(strptime(out))
    except Exception:
        try:
            return os.stat(_dev_script()).st_mtime
        except OSError:
            return time.time()


def _pid_alive(pid: int) -> bool:
    """True iff `pid` is a live process we can signal — used to derive
    instance status (pid present + REST ok → running; pid present but no
    REST yet → starting; pid gone → down). Returns False on bad input."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        # PermissionError means the process exists but isn't ours; in that
        # rare case we still consider it alive.
        return True
    except OSError:
        return False


# ── domain shaping ─────────────────────────────────────────────────────

def _tier(stats: dict) -> str:
    samples = stats.get("samples") or 0
    pf = stats.get("profit_factor") or 0.0
    recent = stats.get("recent_pf") or 0.0
    if samples and recent < LADDER["pf_kill"]:
        return "killed"
    if samples >= LADDER["full_samples"] and pf >= LADDER["pf_pass"]:
        return "full"
    if samples >= LADDER["probe_samples"]:
        return "probe"
    return "base"


POSITION_OPTIMIZER_JOURNAL_KINDS = {
    "position-optimizer-sweep", "position-optimizer", "position-optimizer-applied"}


def position_sweeps_payload(limit=25):
    """Last position-optimizer journal events from state.json's ring
    (newest first, capped at `limit`). Fail-soft: a missing/corrupt state
    file yields [] — the UI shows its empty state, never a 500."""
    try:
        journal = _load_state().get("journal") or []
    except Exception:
        return []
    sweeps = [e for e in journal
              if isinstance(e, dict)
              and e.get("kind") in POSITION_OPTIMIZER_JOURNAL_KINDS]
    return sweeps[-limit:][::-1]


def reliability_archive_payload(limit_per_archetype=20) -> dict:
    """Recent closed round-trips per archetype — derived LIVE from the
    fleet's dry-run trades DBs (the M4 pairing trips: one row per
    completed grid line round-trip). Falls back to the WT-era archive
    file only while the fleet has produced no trips yet."""
    live = _reliability_live()
    arch = {}
    for name, trips in (live.get("trips") or {}).items():
        slim = []
        for t in (trips or []):
            if not isinstance(t, dict):
                continue
            hold_s = None
            if isinstance(t.get("close_ts"), (int, float)) \
                    and isinstance(t.get("entered_at"), (int, float)):
                hold_s = t["close_ts"] - t["entered_at"]
            slim.append({
                "ts": t.get("close_ts"),
                "symbol": t.get("symbol") or "",
                "venue": "hyperliquid",
                "realized": t.get("pnl_usd"),
                "hold_s": hold_s,
                "is_panic": False,
                "is_synthetic": bool(t.get("synthetic")),
                "strategy_id": t.get("strategy_id"),
                "kind": t.get("kind"),
                "gain_pct": t.get("gain_pct"),
            })
        slim.sort(key=lambda r: r.get("ts") or 0, reverse=True)
        arch[name] = slim[:max(1, min(limit_per_archetype, 100))]
    source = "engine trades DBs (live)"
    if not any(arch.values()):
        # pre-reset fallback: the WT-era archive file on disk
        source = "reliability_archive.json (WT-era)"
        archive = _read_json(os.path.join(STATE_DIR,
                                          "reliability_archive.json"),
                             None)
        if isinstance(archive, dict):
            arch = {a: [dict(t, synthetic=bool(t.get("synthetic"))
                             or str(t.get("strategy_id") or "")
                             .startswith("backfill"))
                        for t in rows if isinstance(t, dict)]
                    for a, rows in archive.items()}
            out = {}
            for name, rows in arch.items():
                slim = []
                for t in rows or []:
                    if not isinstance(t, dict):
                        continue
                    slim.append({
                        "ts": t.get("close_ts") or t.get("ts") or t.get("at_epoch"),
                        "symbol": t.get("symbol") or "",
                        "venue": t.get("venue") or "",
                        "realized": (t.get("realized_usd") or t.get("pnl_usd")
                                     or t.get("pnl") or t.get("realized")),
                        "hold_s": t.get("hold_s"),
                        "is_panic": bool(t.get("is_panic") or t.get("panic")),
                        "is_synthetic": bool(t.get("synthetic")),
                        "strategy_id": t.get("strategy_id") or t.get("bot_code"),
                    })
                slim.sort(key=lambda r: r.get("ts") or 0, reverse=True)
                out[name] = slim[:max(1, min(limit_per_archetype, 100))]
            arch = out
    if not any(arch.values()):
        # nothing closed yet and no WT-era file either — honest empty state
        source = "none yet — live; fills as the fleet closes grid trips"
    return {"archetypes": arch, "source": source}


def reliability_payload() -> dict:
    """Reliability ledger — LIVE-first. The primary table is computed from
    the fleet's dry-run trades DBs (see _reliability_live): this is the
    standalone system's own evidence and it fills as grid trips close.
    The WT-era file snapshot (state/reliability.json) is served as a
    labeled secondary block for as long as it exists on disk."""
    path = os.path.join(STATE_DIR, "reliability.json")
    file_ledger = _read_json(path, {}) or {}
    live = _reliability_live()

    def _enrich(ledger: dict) -> dict:
        archs = {}
        for arch, st in ledger.items():
            if isinstance(st, dict):
                st = dict(st)
                st["tier"] = _tier(st)
                synth = st.get("synthetic_samples")
                st["real_samples"] = (max(0, (st.get("samples") or 0)
                                          - synth)
                                      if isinstance(synth, (int, float))
                                      else st.get("samples"))
                samples = st.get("samples") or 0
                probe = LADDER["probe_samples"]
                full = LADDER["full_samples"]
                if st["tier"] == "base":
                    st["ladder_next"] = "probe"
                    st["ladder_next_at"] = probe
                    st["ladder_progress_pct"] = round(
                        min(100, (samples / probe) * 100), 1)
                elif st["tier"] == "probe":
                    st["ladder_next"] = "full"
                    st["ladder_next_at"] = full
                    st["ladder_progress_pct"] = round(
                        min(100, (samples / full) * 100), 1)
                elif st["tier"] == "full":
                    st["ladder_next"] = None
                    st["ladder_next_at"] = None
                    st["ladder_progress_pct"] = 100.0
                else:                    # killed
                    st["ladder_next"] = None
                    st["ladder_next_at"] = None
                    st["ladder_progress_pct"] = 0.0
                archs[arch] = st
        return archs

    archs = _enrich(live.get("ledger") or {})
    note = ("computed live from the fleet's dry-run trades DBs — samples "
            "are closed grid round-trips" if archs else
            "no closed round-trips yet — the ledger fills as the dry-run "
            "fleet completes its first grid trips")
    if live.get("error"):
        note = live["error"]
    return {
        "ladder": LADDER,
        "kill_thresholds": {
            "kill_min_samples": 10,
            "recent_window": 20,
            "live_min_samples": 30,
        },
        "archetypes": archs,
        "note": note,
        "source": "engine trades DBs (live)",
        "engine_freshness": live.get("freshness"),
        "freshness": live.get("freshness") or _freshness(
            path, "WT-era ledger file"),
        # WT-era snapshot, kept visible (labeled) while it exists on disk
        "file_ledger": _enrich(file_ledger),
        "file_freshness": (_freshness(path, "WT-era ledger file")
                           if file_ledger else None),
    }


def _decision_index() -> dict:
    """decisions.jsonl by id → record; cheap full scan, fail-soft empty.

    Powers the slot-card "decision evidence" lookup and the
    /api/decisions/<id> endpoint. ~1k decisions × few hundred bytes
    each = well under a millisecond on the stdlib JSON parser."""
    idx = {}
    try:
        for line in _tail(os.path.join(STATE_DIR, "decisions.jsonl")).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            did = r.get("id")
            if did:
                idx[did] = r
    except OSError:
        pass
    return idx


def _screen_fit_index() -> dict:
    """screen_cache candidates keyed by (venue, symbol) → candidate dict.

    The latest screen's tvcli_fit + confluence_bonus is what the active
    bot was selected ON, so the Fleet slot cards should show it. When
    the cache is older than `optimizer.screen_cache_fresh_min` (default
    120m), the index still serves the last-known fitness with an
    `at_age_min` field so the UI can flag a stale read."""
    idx = {}
    sc = _read_json(os.path.join(STATE_DIR, "state.json"), {}) or {}
    cache = sc.get("screen_cache") or {}
    age_min = None
    at = cache.get("at")
    if at:
        try:
            age_min = round((time.time() - float(at)) / 60.0, 1)
        except (TypeError, ValueError):
            age_min = None
    for c in (cache.get("candidates") or []):
        if not isinstance(c, dict):
            continue
        key = f"{c.get('venue')}:{c.get('symbol')}"
        # tvcli_fit is the per-skill hunt read (squeeze/chop/mtf/vp/sr/dvi
        # metrics, in a dict). Pass it through verbatim so the slot card
        # and decision evidence panel show the same data — without it the
        # slot card's TVCLI confluence strip renders empty chips even
        # though the screen cache carries the values.
        fit_obj = c.get("tvcli_fit")
        idx[key] = {
            "score_final": c.get("score_final"),
            "score": c.get("score"),
            "regime": c.get("regime"),
            "archetype": c.get("archetype"),
            "tvcli_fit": fit_obj if isinstance(fit_obj, dict) else None,
            "confluence_bonus": c.get("confluence_bonus"),
            "confluence_ok": c.get("confluence_ok"),
            "confluence_notes": c.get("confluence_notes"),
            "spread_pct": c.get("spread_pct"),
            "step_pct": c.get("step"),
            "expected_fills_per_24h": c.get("expected_fills_per_24h"),
            "harvest_net_pct_24h": c.get("harvest_net_pct_24h"),
            "confirm_4h": c.get("confirm_4h"),
            "flags": c.get("flags"),
            "at_age_min": age_min,
        }
    idx["__at_age_min__"] = age_min
    return idx


def _enriched_bots(st: dict) -> list[dict]:
    observe = st.get("last_observe") or {}
    dec_idx = _decision_index()
    fit_idx = _screen_fit_index()
    out = []
    for slot_key, bot in (st.get("active_bots") or {}).items():
        bot = dict(bot or {})
        obs = bot.get("observed") or observe.get(str(slot_key)) or {}
        pol = (bot.get("stagnation_policy") or {})
        stag_if = pol.get("stagnant_if") or {}
        fills, ratio = obs.get("fills_24h"), obs.get("realized_ratio")
        stagnant = None
        if isinstance(fills, (int, float)) and isinstance(ratio, (int, float)):
            min_fills = stag_if.get("min_fills_24h")
            min_ratio = stag_if.get("min_realized_ratio")
            if min_fills is not None and min_ratio is not None:
                stagnant = fills < min_fills and ratio < min_ratio
        # tvcli_fit: the screen-time confluence read the bot was selected on.
        # Joined from the latest screen cache by venue+symbol — when absent
        # (bot not in the latest cache, e.g. deployed on a prior rescreen
        # that's since rolled off), the keys are just null and the UI
        # degrades to "—".
        fit = fit_idx.get(f"{bot.get('venue')}:{bot.get('symbol')}") or {}
        # decision evidence: full record so the Fleet rail can render the
        # debate + risk-team + confluence without a second round-trip.
        dec_id = bot.get("decision_id")
        dec = dec_idx.get(dec_id) if dec_id else None
        # position-optimizer summary: the slow lane's last analysis.
        po = bot.get("position_optimizer") or {}
        out.append({
            "slot": int(slot_key) if str(slot_key).isdigit() else slot_key,
            "symbol": bot.get("symbol"), "venue": bot.get("venue"),
            "grid_type": (bot.get("ticket") or {}).get("grid_type"),
            "since": bot.get("since"), "adopted": bool(bot.get("adopted")),
            "bot_code": bot.get("bot_code"), "channel": bot.get("channel"),
            "archetype": bot.get("archetype"),
            "score_final": bot.get("score_final"),
            "decision_id": dec_id,
            "decision": dec,
            "tvcli_fit": fit.get("tvcli_fit"),
            "tvcli_bonus": fit.get("confluence_bonus"),
            "tvcli_ok": fit.get("confluence_ok"),
            "tvcli_notes": fit.get("confluence_notes"),
            "screen_score": fit.get("score_final"),
            "screen_age_min": fit.get("at_age_min"),
            "expected_fills_24h": fit.get("expected_fills_per_24h"),
            "harvest_net_pct_24h": fit.get("harvest_net_pct_24h"),
            "force_rotate": bool(bot.get("force_rotate")),
            "needs_reanalysis": bool(bot.get("needs_reanalysis")),
            "committed": (st.get("committed") or {}).get(str(slot_key)),
            "stagnation_policy": pol,
            "observed": obs,
            "stagnant": stagnant,
            "position_optimizer": po,
            "optimizer_tracker": bot.get("optimizer"),
            # current exit profile (enriched grid_list fields projected
            # by the daemon health cycle / observe layer) — renders the
            # exit badge on the fleet card when present
            "exits": (bot.get("exits") if isinstance(bot.get("exits"), dict)
                      else (obs.get("exits")
                            if isinstance(obs.get("exits"), dict) else None)),
            "take_profit_usd": bot.get("take_profit_usd"),
            "loss_veto": obs.get("loss_veto") if isinstance(obs, dict) else None,
        })
    out.sort(key=lambda b: (not str(b["slot"]).isdigit(), str(b["slot"])))
    return out


def _latest_report_meta(kind: str):
    """(report, stem) of the most recent run card of this kind, or
    (None, None). The stem is what the console hands to /api/reports/<stem>
    for the rail-card deep-link, so the operator can jump from a screen
    rank to the deliberation that produced it without re-typing the ts."""
    rdir = os.path.join(STATE_DIR, "reports")
    try:
        names = sorted((n for n in os.listdir(rdir)
                        if n.endswith(".json") and f"-{kind}." in n), reverse=True)
    except OSError:
        return None, None
    for name in names:
        rep = _read_json(os.path.join(rdir, name))
        if rep is not None:
            return rep, os.path.splitext(name)[0]
    return None, None


def _latest_report(kind: str):
    rep, _ = _latest_report_meta(kind)
    return rep


def _screen_score_history(limit: int) -> list[dict]:
    """Top-of-screen `score_final` over the last N rescreen cards, oldest
    first. Powers the "last screen" rail's trend sparkline. Returns
    [{at, score, n_candidates}] for each card; [] when fewer than two
    rescreens exist (a one-point sparkline is just a dot)."""
    rdir = os.path.join(STATE_DIR, "reports")
    try:
        names = sorted((n for n in os.listdir(rdir)
                        if n.endswith(".json") and "-rescreen." in n),
                       reverse=True)[:max(1, limit)]
    except OSError:
        return []
    out = []
    for n in names:
        rep = _read_json(os.path.join(rdir, n))
        if not isinstance(rep, dict):
            continue
        scr = rep.get("screen") or {}
        top = (scr.get("top3") or [])
        score = top[0].get("score_final") if top and isinstance(top[0], dict) else None
        out.append({"at": rep.get("at"),
                    "score": score,
                    "n_candidates": scr.get("n_candidates")})
    out.reverse()
    return [r for r in out if r.get("at") and _is_num(r.get("score"))]


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def screen_payload() -> dict | None:
    rep, stem = _latest_report_meta("rescreen")
    if not rep:
        return None
    scr = rep.get("screen") or {}
    return {
        "at": rep.get("at"), "cycle_kind": rep.get("cycle_kind"),
        "n_candidates": scr.get("n_candidates"),
        "top": scr.get("top3") or [],
        "hunt_stats": scr.get("hunt_stats") or {},
        "data_sources": rep.get("data_sources") or {},
        "run_card_stem": stem,
        # 12-point score history for the rail's "is screening improving?"
        # sparkline: top-of-screen score_final across the last 12 rescreen
        # run cards. Cheap (file scan + JSON parse) and powers a glanceable
        # trend. Empty list = fewer than 2 rescreen cards on disk.
        "score_history": _screen_score_history(12),
        # the run-card JSON keys are deliberate/guard (singular, as the
        # daemon writes them): pass them through verbatim so the UI can
        # show the per-cycle deliberation + guard verdict
        "deliberations": rep.get("deliberations") or [],
        "guard": rep.get("guard") or [],
        "deployments": rep.get("deployments") or [],
        "rotations": rep.get("rotations") or [],
        "actions": rep.get("actions") or [],
        "caveats": rep.get("caveats") or [],
        "dry_run": rep.get("dry_run"),
    }


def decision_payload_by_id(decision_id: str) -> dict | None:
    """One full decision record by id, plus context.

    Powers the Fleet slot-card "View decision evidence" deep-link and the
    /api/decisions/<id> endpoint. Fails soft with None when missing —
    the UI shows a stale-by-id note instead of a 500."""
    if not decision_id:
        return None
    idx = _decision_index()
    row = idx.get(decision_id)
    if row is None:
        return None
    # Sibling decisions for the same symbol + venue + regime — the cohort
    # the k=3 memory recall drew from, surfaced so the UI can answer
    # "what happened last time we ran this archetype here" without
    # scanning the ledger. Identity filter is by `id` (each call to
    # _decision_index returns fresh objects, so `is` would always match).
    sib = [r for r in idx.values()
           if r.get("id") != decision_id
           and r.get("symbol") == row.get("symbol")
           and r.get("venue") == row.get("venue")
           and r.get("regime") == row.get("regime")]
    sib.sort(key=lambda r: r.get("at") or "", reverse=True)
    return {"decision": row,
            "cohort_size": len(sib),
            "cohort_realized": sum(
                float(((r.get("outcome") or {}).get("realized_pnl")) or 0)
                for r in sib if isinstance(r.get("outcome"), dict))}


def optimizer_swap_log() -> dict:
    """The fast slot-optimizer's swap_log + per-slot trackers + last
    arbiter verdict — surfaced in the console so an operator can answer:

      * which slots have been swapped and when
      * how long each slot has been idle (last_fills / last_increase_at)
      * what the Mistral arbiter last concluded per idle slot
      * how many cycles the optimizer has run + swap totals

    All from state.optimizer (already in-memory, no extra I/O); the
    arbiter verdict comes from the last cycle report's `arbiter` block.
    """
    st = _read_json(os.path.join(STATE_DIR, "state.json"), {}) or {}
    opt = st.get("optimizer") or {}
    trackers = opt.get("trackers") or {}
    swap_log = opt.get("swap_log") or []
    last = opt.get("last_report") or {}
    now = time.time()
    tracker_rows = []
    for slot, tr in trackers.items():
        if not isinstance(tr, dict):
            continue
        last_inc = tr.get("last_increase_at")
        idle_min = None
        if isinstance(last_inc, (int, float)) and last_inc > 0:
            idle_min = round((now - float(last_inc)) / 60.0, 1)
        tracker_rows.append({
            "slot": slot,
            "last_fills": tr.get("last_fills"),
            "last_increase_at": last_inc,
            "idle_min": idle_min,
        })
    tracker_rows.sort(key=lambda r: (r["idle_min"] is None,
                                      r["idle_min"] if r["idle_min"] is not None else 0),
                      reverse=True)
    swaps = []
    for e in swap_log:
        if not isinstance(e, dict):
            continue
        swaps.append({
            "slot": e.get("slot"),
            "at": e.get("at"),
            "ok": bool(e.get("ok")),
            "at_iso": (datetime.fromtimestamp(float(e["at"]), tz=timezone.utc).isoformat(timespec="seconds")
                       if isinstance(e.get("at"), (int, float)) and e["at"] > 0 else None),
        })
    swaps.sort(key=lambda s: s.get("at") or 0, reverse=True)
    arbiter = last.get("arbiter") if isinstance(last, dict) else None
    return {
        "cycles": opt.get("cycles") or 0,
        "swaps_total": opt.get("swaps_total") or 0,
        "cycles_since_card": opt.get("cycles_since_card") or 0,
        "last_at": opt.get("last_at"),
        "trackers": tracker_rows,
        "swaps": swaps[:60],
        "last_arbiter": arbiter,
        "caveats": (last.get("caveats") or []) if isinstance(last, dict) else [],
    }


def llm_health() -> dict:
    """Live LLM provider reachability + the role-pinning matrix, served
    without exposing any keys.

    The same `llm/provider.py --ping` the console's "validate" button
    uses, but cached for 60s so the Fleet rail / readiness strip / a
    dedicated panel can poll every 5s without hammering the providers.
    Surfaces the role routing (which swarm agent uses which provider)
    so the operator can see at a glance whether Mistral is actually
    driving the fast-lane arbiter — the headline use case."""
    cache_key = "llm_health"
    hit = _CTL_CACHE.get(cache_key)
    if hit and hit[0] > time.time():
        return hit[2]

    provider_script = os.path.join(GRID_HOME, "llm", "provider.py")
    ping_results = []
    chain = []
    if os.path.isfile(provider_script):
        env = dict(os.environ)
        for key, val in _llm_sidecar().items():
            env[key] = val
        try:
            proc = subprocess.run(
                [sys.executable, provider_script, "--ping", "--json"],
                capture_output=True, text=True, timeout=180, env=env,
                cwd=GRID_HOME)
            if proc.returncode == 0:
                try:
                    data = json.loads(proc.stdout)
                    chain = data.get("chain") or []
                    for r in (data.get("results") or []):
                        ping_results.append({
                            "provider": r.get("provider"),
                            "ok": bool(r.get("ok")),
                            "latency_ms": r.get("latency_ms"),
                            "error": (str(r.get("error", ""))[:160]
                                      if not r.get("ok") else None),
                        })
                except Exception:
                    pass
        except subprocess.TimeoutExpired:
            ping_results = [{"provider": p, "ok": False, "error": "ping timeout (180s)"}
                            for p in ("cf", "nvidia", "openrouter", "mistral")]
        except Exception as exc:
            ping_results = [{"provider": "?", "ok": False,
                             "error": f"ping failed: {str(exc)[:120]}"}]

    # fall back to presence-only when the ping subprocess failed
    if not ping_results:
        side = _llm_sidecar()
        for name in LLM_PROVIDERS:
            kenv = {"cf": ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_KEY", "CLOUDFLARE_AI_TOKEN"),
                    "nvidia": ("NVIDIA_API_KEY",),
                    "openrouter": ("OPENROUTER_API_KEY",),
                    "mistral": ("MISTRAL_API_KEY",)}[name]
            present = any(side.get(k) or os.environ.get(k) for k in kenv)
            ping_results.append({"provider": name, "ok": present,
                                 "error": None if present else "no key"})

    # role pinning (mirrors llm_state from /api/llm but re-parsed here so
    # the operator sees WHICH model is driving EACH agent in the swarm —
    # the headline "is Mistral actually doing the arbiter?" question)
    side = _llm_sidecar()
    raw_roles = side.get("GRID_LLM_ROLES")
    roles = {}
    if raw_roles:
        try:
            parsed = json.loads(raw_roles)
            if isinstance(parsed, dict):
                roles = parsed
        except Exception:
            roles = {}

    # active LLM provider for the fast-lane arbiter, mirrored from
    # config.optimizer.llm_provider (with sensible default to "mistral")
    cfg = (config_payload().get("config") or {})
    arbiter_provider = ((cfg.get("optimizer") or {}).get("llm_provider")
                        or "mistral")

    out = {
        "at": utcnow(),
        "chain": chain,
        "results": ping_results,
        "roles": roles,
        "role_keys": LLM_ROLE_KEYS,
        "arbiter_provider": arbiter_provider,
        "note": ("Live ping cached 60s; keys never returned. "
                 "Arbiter default = mistral (override via "
                 "config.optimizer.llm_provider)."),
    }
    _CTL_CACHE[cache_key] = (time.time() + 60.0, True, out)
    return out


def decisions_payload(limit: int) -> list[dict]:
    text = _tail(os.path.join(STATE_DIR, "decisions.jsonl"))
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    rows.sort(key=lambda r: r.get("at") or "", reverse=True)
    return rows[:max(1, min(limit, 1000))]


def recommendations_payload(limit: int) -> dict:
    """Position-optimizer recommendations from the PocketBase side channel
    (newest first). Non-fatal: an empty list when PB is down or the
    collection does not exist yet. Sorted by the engine's ISO `at` field —
    PB 0.40 has no auto `created` system field, so `sort=-created` 400s.

    Auth: the collection rules block public reads, so this goes through the
    same ladder as pnl_payload — pbclient (pb.env-seeded superuser re-auth),
    then raw HTTP with the sidecar's stored PB_TOKEN. A bare unauthenticated
    read used to 401/404 here, so the view always looked empty even when
    records existed."""
    limit = max(1, min(limit, 500))
    items = None
    pb = _pb_client()
    if pb is not None:
        try:
            items = pb.list("recommendations", sort="-at",
                            per_page=limit)
        except Exception:
            items = None
    if not items:
        url = (f"{PB_URL}/api/collections/recommendations/records"
               f"?perPage={limit}&sort=-at")
        ok, body = _pb_get(url)
        items = (body or {}).get("items") if ok and isinstance(body, dict) else None
        if not items:
            token = _pb_env().get("PB_TOKEN")
            if token:
                ok, body = _pb_get(url, token=token)
                items = (body or {}).get("items") \
                    if ok and isinstance(body, dict) else None
    items = [dict(r) for r in items if isinstance(r, dict)] \
        if isinstance(items, list) else []
    source = "pocketbase"
    if not items:
        # Journal fallback: a dry-run mirror never persists (persist is
        # gated on not-dry_run) but DOES journal every recommendation —
        # without this the Optimizer view looked permanently empty on the
        # az00 mirror even though the engine emits recs every sweep.
        # Derived rows carry journal_only + a source marker; they are NOT
        # applyable (no PB record to flip applied on).
        evs = [e for e in (_load_state().get("journal") or [])
               if e.get("kind") == "position-optimizer"
               and e.get("recommendation")]
        evs.sort(key=lambda e: e.get("at") or "", reverse=True)
        items = []
        for e in evs[:limit]:
            venue = ""
            parts = (e.get("msg") or "").split(" ", 1)
            if len(parts) == 2 and ":" in parts[1]:
                venue = parts[1].split(":")[0]
            items.append({
                "at": e.get("at"), "slot": e.get("slot"),
                "venue": venue, "symbol": e.get("symbol"),
                "recommendation": e.get("recommendation"),
                "expected_delta_pct": e.get("expected_delta_pct"),
                "trigger": e.get("trigger"),
                "dry_run": bool(e.get("dry_run")),
                "applied": False, "applied_at": None,
                "blocked_by": "journal-only",
                "journal_only": True,
            })
        if items:
            source = "journal"

    # Enrich each record with the apply-gate verdict so the UI can say WHY
    # a recommendation is sitting unapplied: config `apply: false` (advisory
    # mode), the persisted-per-day cap, or already applied.
    cfg = (config_payload().get("config") or {}).get("position_optimizer") or {}
    apply_enabled = bool(cfg.get("apply"))
    max_day = cfg.get("max_apply_per_day") or 4
    today = utcnow()[:10]
    persisted_today = sum(1 for r in items
                          if str(r.get("at") or "").startswith(today)
                          and not r.get("journal_only"))
    for r in items:
        r.setdefault("applied", False)
        r.setdefault("applied_at", None)
        if r.get("journal_only"):
            continue
        if r.get("applied"):
            r["blocked_by"] = "applied"
        elif not apply_enabled:
            r["blocked_by"] = "apply disabled"
        elif persisted_today >= max_day:
            r["blocked_by"] = "rate limit"
        else:
            r["blocked_by"] = ""
    return {"recommendations": items, "apply": apply_enabled,
            "max_apply_per_day": max_day, "persisted_today": persisted_today,
            "source": source}


def reports_index() -> list[dict]:
    rdir = os.path.join(STATE_DIR, "reports")
    try:
        names = os.listdir(rdir)
    except Exception:
        return []
    stems = {}
    for n in names:
        base, ext = os.path.splitext(n)
        if ext in (".json", ".md"):
            stems.setdefault(base, {})[ext[1:]] = True
    out = []
    for stem, has in sorted(stems.items(), reverse=True):
        m = re.match(r"^(\d{8}T\d{6}Z)-(.+)$", stem)
        at = None
        if m:
            try:
                at = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ") \
                    .replace(tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
        out.append({"stem": stem, "kind": m.group(2) if m else stem,
                    "at": at, "json": bool(has.get("json")),
                    "md": bool(has.get("md"))})
    return out[:300]


def logs_payload(lines: int, grep: str | None) -> dict:
    # the supervisor decides the sink (launchd in-process redirect vs the
    # manual start.sh log) — read whichever is authoritative, and report it
    log = _log_source()
    # Standalone workspace path: a virtual concatenated tail of every
    # active log file (console + per-instance freqtrade engines).
    if log.get("source") == "workspace-tail":
        rows = (log.get("lines_text") or "").splitlines()
        if grep:
            pat = re.compile(grep, re.IGNORECASE)
            rows = [r for r in rows if pat.search(r)]
        return {
            "lines": rows[-max(1, min(lines, 4000)):],
            "total": len(rows),
            "path": log.get("state_log"),
            "source": "workspace-tail",
            "sources": log.get("sources", []),
            "note": log.get("note"),
        }
    text = _tail(log["path"], 1024 * 1024)
    rows = text.splitlines()
    if grep:
        pat = re.compile(grep, re.IGNORECASE)
        rows = [r for r in rows if pat.search(r)]
    return {"lines": rows[-max(1, min(lines, 2000)):], "total": len(rows),
            "path": log["path"], "source": log["source"]}


def config_payload() -> dict:
    cfg = {}
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg = load_yaml(f.read()) or {}
        except Exception:
            cfg = {}
    editable = {}
    with open(CONFIG_PATH) as f:
        text = f.read()
    for path, rule in EDITABLE.items():
        val, ok = yaml_edit.get_value(text, path)
        editable[path] = {**rule, "value": val if ok else None, "present": ok}
    return {"config": cfg, "editable": editable,
            "note": "The daemon reads config.yaml at startup — applied edits "
                    "need a daemon restart to take effect.",
            "freshness": _freshness(CONFIG_PATH, "config.yaml")}


def apply_config_edits(edits: dict) -> tuple[int, dict]:
    if not isinstance(edits, dict) or not edits:
        return 400, {"error": "body must be {edits: {path: value}}"}
    if not os.path.isfile(CONFIG_PATH):
        return 500, {"error": f"config.yaml not found at {CONFIG_PATH}"}
    with open(CONFIG_PATH) as f:
        text = f.read()
    applied, rejected = [], []
    for path, value in edits.items():
        rule = EDITABLE.get(path)
        if rule is None:
            rejected.append({"path": path, "reason": "not editable via console"})
            continue
        try:
            if rule["t"] == "int":
                value = int(value)
            elif rule["t"] == "bool":
                if isinstance(value, bool):
                    pass
                elif isinstance(value, (int, float)):
                    value = bool(value)
                else:
                    s = str(value).strip().lower()
                    if s not in ("true", "false", "1", "0", "yes", "no",
                                 "on", "off"):
                        raise ValueError(value)
                    value = s in ("true", "1", "yes", "on")
            else:
                value = float(value)
        except (TypeError, ValueError):
            rejected.append({"path": path, "reason": f"expected {rule['t']}"})
            continue
        # bounds are optional (bool knobs and future rule types carry none)
        lo, hi = rule.get("min"), rule.get("max")
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            rejected.append({"path": path,
                             "reason": f"out of range [{lo}, {hi}]"})
            continue
        new_text = yaml_edit.set_value(text, path, value)
        if new_text is None:
            rejected.append({"path": path, "reason": "path not found in config.yaml"})
            continue
        text = new_text
        applied.append({"path": path, "value": value})
    if not applied:
        return 400, {"applied": [], "rejected": rejected}
    # round-trip guard: the edited file must still parse and carry the values
    parsed = load_yaml(text)
    for a in applied:
        node = parsed
        for part in a["path"].split("."):
            node = (node or {}).get(part)
        if node != a["value"]:
            return 500, {"error": f"round-trip check failed for {a['path']}"}
    backup = CONFIG_PATH + ".bak"
    try:
        shutil.copy2(CONFIG_PATH, backup)
    except Exception:
        pass
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, CONFIG_PATH)
    return 200, {"applied": applied, "rejected": rejected,
                 "backup": backup, "restart_required": True}


# ── LLM provider sidecar (set / choose / validate) ─────────────────────

def _llm_sidecar() -> dict:
    """Parse state/llm.env into {KEY: value}; {} when absent/unparseable.

    Values (incl. keys) are read here but only ever used internally — the
    API surfaces presence booleans and models, never a key's value.
    """
    if not os.path.isfile(LLM_ENV_PATH):
        return {}
    out = {}
    try:
        with open(LLM_ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.removeprefix("export ").partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key:
                    out[key] = val
    except OSError:
        pass
    return out


def _llm_env_value(key: str, default: str) -> str:
    """Sidecar first, live env fallback, then the module default."""
    side = _llm_sidecar()
    if key in side:
        return side[key]
    if os.environ.get(key):
        return os.environ[key]
    return default


def llm_payload() -> dict:
    side = _llm_sidecar()
    # Models: sidecar wins, then live env, then provider.py defaults.
    model_defaults = {
        "cf": os.environ.get("CF_MODEL", "@cf/zai-org/glm-5.3"),
        "nvidia": os.environ.get("NVIDIA_MODEL", "meta/llama-3.3-70b-instruct"),
        "openrouter": os.environ.get("OPENROUTER_MODEL",
                                     "arcee-ai/trinity-large-preview:free"),
        "mistral": os.environ.get("MISTRAL_MODEL", "mistral-large-latest"),
    }
    key_env = {
        "cf": ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_KEY", "CLOUDFLARE_AI_TOKEN"),
        "nvidia": ("NVIDIA_API_KEY",),
        "openrouter": ("OPENROUTER_API_KEY",),
        "mistral": ("MISTRAL_API_KEY",),
    }
    chain = side.get("GRID_LLM_CHAIN") or os.environ.get(
        "GRID_LLM_CHAIN", "cf,nvidia,openrouter,mistral")
    chain_list = [p.strip() for p in chain.split(",") if p.strip()]

    def _present(name):
        for key in key_env[name]:
            if side.get(key) or os.environ.get(key):
                return True
        return False

    providers = {}
    for name in LLM_PROVIDERS:
        model_var = {"cf": "CF_MODEL", "nvidia": "NVIDIA_MODEL",
                     "openrouter": "OPENROUTER_MODEL", "mistral": "MISTRAL_MODEL"}[name]
        providers[name] = {
            "key_present": _present(name),
            "model": side.get(model_var) or model_defaults[name],
            "model_env": model_var,
            "chain_position": (chain_list.index(name)
                               if name in chain_list else None),
            "enabled": name in chain_list,
        }
    roles = {}
    raw_roles = side.get("GRID_LLM_ROLES")
    if raw_roles:
        try:
            parsed = json.loads(raw_roles)
            if isinstance(parsed, dict):
                roles = parsed
        except Exception:
            roles = {}
    return {
        "providers": providers,
        "chain": chain_list,
        "roles": roles,
        "role_keys": LLM_ROLE_KEYS,
        "llm_env": {"cf": _present("cf"), "nvidia": _present("nvidia"),
                    "openrouter": _present("openrouter"),
                    "mistral": _present("mistral")},
        "sidecar": os.path.isfile(LLM_ENV_PATH),
        "note": "Keys are stored in state/llm.env (workspace, 0600) and "
                "never returned. In this standalone workspace the "
                "freqtrade engine makes no LLM calls — the swarm consumer "
                "is the autonomy brain (M3, in flight), which reads the "
                "sidecar at its next LLM call.",
    }


def apply_llm(updates: dict) -> tuple[int, dict]:
    """Persist provider models, chain order, keys, and role routing to the sidecar.

    Body: {"providers": {name: {model?, key?}}, "chain": [..], "roles": {..}}.
    A `key` value of "" or "__KEEP__" leaves the stored key untouched; any
    other non-empty string replaces it. Models/chain/roles are written verbatim
    (model strings are free-form; chain entries must be known providers).
    """
    side = _llm_sidecar()

    providers = updates.get("providers") or {}
    if not isinstance(providers, dict):
        return 400, {"error": "providers must be an object"}
    chain = updates.get("chain")
    roles = updates.get("roles")

    # Merge models + keys.
    for name, spec in providers.items():
        if name not in LLM_PROVIDERS or not isinstance(spec, dict):
            continue
        model_var = {"cf": "CF_MODEL", "nvidia": "NVIDIA_MODEL",
                     "openrouter": "OPENROUTER_MODEL", "mistral": "MISTRAL_MODEL"}[name]
        if "model" in spec:
            model = str(spec["model"]).strip()
            if model:
                side[model_var] = model
        if "key" in spec:
            key_val = str(spec["key"])
            key_var = {"cf": "CLOUDFLARE_API_KEY", "nvidia": "NVIDIA_API_KEY",
                       "openrouter": "OPENROUTER_API_KEY",
                       "mistral": "MISTRAL_API_KEY"}[name]
            if key_val == "__CLEAR__":
                # explicit delete: drop the key from the sidecar so the
                # provider falls out of the chain on the next self-heal.
                side.pop(key_var, None)
            elif key_val and key_val != "__KEEP__":
                side[key_var] = key_val
            # "" / "__KEEP__" → leave existing key (or none) untouched.

    # Chain order: validate names, dedupe, append any omitted enabled providers.
    if chain is not None:
        if not isinstance(chain, list):
            return 400, {"error": "chain must be a list"}
        clean = []
        for p in chain:
            if p in LLM_PROVIDERS and p not in clean:
                clean.append(p)
        for p in LLM_PROVIDERS:
            if p not in clean:
                clean.append(p)
        side["GRID_LLM_CHAIN"] = ",".join(clean)

    # Roles: validate keys + provider values; drop unknown.
    if roles is not None:
        if not isinstance(roles, dict):
            return 400, {"error": "roles must be an object"}
        clean_roles = {}
        for role, provider in roles.items():
            if role in LLM_ROLE_KEYS and provider in LLM_PROVIDERS:
                clean_roles[role] = provider
        side["GRID_LLM_ROLES"] = json.dumps(clean_roles)

    # Serialize back to "export K=\"v\"" lines, atomic + backup + 0600.
    lines = []
    for key, val in side.items():
        lines.append(f'export {key}="{val}"')
    text = "\n".join(lines) + "\n"

    try:
        os.makedirs(os.path.dirname(LLM_ENV_PATH), exist_ok=True)
        if os.path.isfile(LLM_ENV_PATH):
            shutil.copy2(LLM_ENV_PATH, LLM_ENV_PATH + ".bak")
    except OSError:
        pass
    tmp = LLM_ENV_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, LLM_ENV_PATH)
    except OSError as exc:
        return 500, {"error": f"write failed: {exc}"}

    return 200, {"applied": True, "providers": list(providers),
                 "chain": side.get("GRID_LLM_CHAIN", "").split(","),
                 "roles": json.loads(side.get("GRID_LLM_ROLES", "{}")),
                 "note": "Saved to state/llm.env (workspace, 0600). "
                         "Consumed by the autonomy brain when it lands "
                         "(M3); the standalone freqtrade engine makes no "
                         "LLM calls."}


def validate_llm() -> tuple[int, dict]:
    """Live ping each provider via `llm/provider.py --ping --json`, sourcing
    the sidecar into the child env so validation matches runtime exactly.
    Never returns key values — only ok/latency/error.
    """
    provider_script = os.path.join(GRID_HOME, "llm", "provider.py")
    if not os.path.isfile(provider_script):
        return 500, {"error": "llm/provider.py not found"}
    env = dict(os.environ)
    for key, val in _llm_sidecar().items():
        env[key] = val
    try:
        proc = subprocess.run(
            [sys.executable, provider_script, "--ping", "--json"],
            capture_output=True, text=True, timeout=90, env=env,
            cwd=GRID_HOME)
    except subprocess.TimeoutExpired:
        return 504, {"error": "ping timed out after 90s"}
    except Exception as exc:
        return 500, {"error": f"ping failed: {exc}"}
    if proc.returncode != 0:
        return 502, {"error": "provider.py --ping exited "
                              f"{proc.returncode}: {(proc.stderr or '')[:200]}"}
    try:
        data = json.loads(proc.stdout)
    except Exception:
        return 500, {"error": "unparseable ping output"}
    results = data.get("results") or []
    # Strip any key material defensively — ping() already emits none, but the
    # "error" field can echo a URL/response; truncate to be safe.
    safe = []
    for r in results:
        safe.append({
            "provider": r.get("provider"),
            "ok": bool(r.get("ok")),
            "latency_ms": r.get("latency_ms"),
            "error": str(r.get("error", ""))[:160] if not r.get("ok") else None,
        })
    return 200, {"chain": data.get("chain", [p for p, _ in
                  [(r.get("provider"), r) for r in safe]]),
                 "results": safe}


# ── execution engine + freqtrade fleet ─────────────────────────────────

def engine_payload() -> dict | None:
    """state/engine.json — the active execution engine (freqtrade since the
    M3 pivot; legacy WunderTrading paper engine closed). Fail-soft: None
    when absent/corrupt, so the UI degrades to its WT-era rendering."""
    try:
        with open(ENGINE_PATH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


# registry instance fields safe to surface in the UI. username/password
# (the per-instance freqtrade api_server credentials) stay in registry.json
# + config.json on the host — they never cross the /api boundary.
_FT_SAFE_FIELDS = (
    "bot_code", "dir", "port", "pair", "pair_code", "symbol", "venue",
    "exchange_code", "grid_type", "slot_balance", "channel", "created_at",
    "pid", "status", "backend", "timeframe",
)


def ft_fleet_payload() -> dict:
    """Freqtrade fleet registry (state/ft_fleet/registry.json), secret-safe:
    instances whitelisted field-by-field; the archived list only ever holds
    {archived_at, archived_to, bot_code} and is capped to the newest 12."""
    path = os.path.join(STATE_DIR, "ft_fleet", "registry.json")
    try:
        with open(path) as f:
            reg = json.load(f)
    except Exception:
        return {"active": 0, "instances": [], "archived_count": 0,
                "archived_tail": []}
    instances = []
    for code, e in (reg.get("instances") or {}).items():
        if isinstance(e, dict):
            inst = {k: e.get(k) for k in _FT_SAFE_FIELDS if k in e}
            # live enrichment: engine REST marks + trades-DB counts +
            # the deployed channel as the strategy computes it. All
            # derived server-side; credentials never enter the payload.
            inst.update(_engine_instance_stats(e))
            inst["channel_live"] = _live_geometry(e)
            # Derive status live — registry.json's "status" is a snapshot
            # from registration time and never updates. The live signal is:
            #   running   — pid is alive AND the engine REST probe succeeded
            #   starting  — pid is alive but REST hasn't come up yet (warm-up)
            #   down      — pid is gone
            pid_alive = bool(e.get("pid")) and _pid_alive(e["pid"])
            inst["status"] = (
                "running" if pid_alive and inst.get("api_ok") else
                "starting" if pid_alive else
                "down"
            )
            instances.append(inst)
    archived = [a for a in (reg.get("archived") or [])
                if isinstance(a, dict)]
    # archived_to is sanitized to a basename: the four archived instance
    # dirs live in the companion repo's archive and full out-of-workspace
    # paths must not cross the /api boundary
    tail = [{k: (os.path.basename(a["archived_to"])
                 if k == "archived_to" and a.get("archived_to")
                 else a.get(k))
             for k in ("archived_at", "archived_to", "bot_code") if k in a}
            for a in archived[-12:]]
    return {
        "active": len(instances),
        "instances": instances,
        "archived_count": len(archived),
        "archived_tail": list(reversed(tail)),  # newest first
    }


# ── live freqtrade-engine probes (workspace-contained end to end) ──────
# The standalone system's live truth: per-instance dry-run trades DBs
# (sqlite artifacts under state/ft_fleet/<bot>/) + the engines' own local
# REST APIs (auth from registry.json — credentials stay server-side and
# never cross the /api boundary). These replace the WT-era PocketBase
# reads and the frozen state.json journal, which can only ever serve
# pre-pivot snapshots.

def _ft_registry() -> dict:
    """state/ft_fleet/registry.json (fail-soft to an empty registry)."""
    reg = _read_json(os.path.join(STATE_DIR, "ft_fleet", "registry.json"),
                     None)
    if isinstance(reg, dict) and isinstance(reg.get("instances"), dict):
        return reg
    return {"instances": {}}


def _iso_db_utc(raw) -> str | None:
    """freqtrade sqlite datetimes are naive UTC ("2026-09-14 22:00:44.234");
    normalize to ISO-8601 Z so every browser parses them as UTC."""
    if not raw:
        return None
    return str(raw).replace(" ", "T") + "Z"


# ── engine-REST cache + back-off ───────────────────────────────────────
# The Fleet view polls /api/overview every ~5s; without caching that is
# 12 engine-REST calls (status/balance/pair_candles × 4 slots) every 5s.
# Worse: a wedged engine (Hyperliquid 429 storm) holds each server-side
# handler ~10-50s — far longer than our 2.5s client timeout — and every
# handler holds a scoped-session DB connection. Re-polling every 5s
# spawned zombie handlers that piled up until freqtrade's QueuePool
# (5 + 10 overflow) exhausted and the bot loop's own read hit the 30s
# checkout timeout (fatal crash, verified 2026-09-15). Two defenses:
#   TTL cache   30s per (bot_code, path) — a 1h-candle grid fleet does
#               not need fresher marks than that.
#   cool-down   60s per instance after ANY REST failure — stop feeding
#               the pileup while the engine recovers; SQLite-derived
#               stats keep flowing during the back-off.
_REST_TTL_S = 30.0
_REST_COOLDOWN_S = 60.0
_REST_CACHE: dict = {}       # (bot_code, path) -> (expires_at, payload)
_REST_COOLDOWN: dict = {}    # bot_code -> expires_at (skip REST until)


def _engine_rest(entry: dict, path: str, timeout: float = 2.5,
                 ttl: float = _REST_TTL_S):
    """GET one freqtrade REST endpoint using the instance's api_server
    credentials (HTTP Basic). Returns parsed JSON, or None on any failure.
    Credentials are used for the Authorization header only — never logged,
    never returned in any payload. Cached `ttl` seconds per instance+path;
    after a failure, REST calls for THAT instance+path back off for
    _REST_COOLDOWN_S — keyed per endpoint, so one hung endpoint does not
    wedge the rest of the instance's REST surface (see module note for the
    pool-exhaustion mechanism)."""
    bot = str(entry.get("bot_code") or entry.get("port") or "?")
    now = time.time()
    # Per-(bot, path) cooldown: a single hung endpoint (e.g. /api/v1/balance
    # on Hyperliquid when the exchange stub is slow) must not wedge other
    # endpoints on the same instance — channel_live (pair_candles) still
    # needs to render while /balance is in back-off.
    key = (bot, path)
    cd = _REST_COOLDOWN.get(key)
    if cd and cd > now:
        return None
    hit = _REST_CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{int(entry['port'])}{path}")
        token = base64.b64encode(
            f"{entry['username']}:{entry['password']}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read() or b"{}")
    except Exception:
        _REST_COOLDOWN[key] = now + _REST_COOLDOWN_S
        _REST_CACHE.pop(key, None)
        return None
    _REST_CACHE[key] = (now + ttl, payload)
    return payload


def _engine_instance_stats(entry: dict) -> dict:
    """Live per-instance facts: sqlite counts/realized + engine REST marks.
    Every source is a workspace artifact or the local engine itself; REST
    is fail-soft (engine down → api_ok False, live marks None)."""
    out = {
        "bot_code": entry.get("bot_code"),
        "api_ok": False, "wallet_total": None,
        "open_trades": 0, "closed_trades": 0, "realized": 0.0,
        "open_stake": 0.0, "fills": 0, "fills_24h": 0,
        "open_profit_abs": None, "open_profit_pct": None,
        "open_enter_tag": None, "last_fill_at": None,
        "started_at": None,
    }
    db = os.path.join(entry.get("dir") or "", "tradesv3.dryrun.sqlite")
    if os.path.isfile(db):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                cur = con.cursor()
                cur.execute("SELECT count(*), coalesce(sum(stake_amount), 0) "
                            "FROM trades WHERE is_open=1")
                open_n, open_stake = cur.fetchone()
                out["open_trades"] = int(open_n or 0)
                out["open_stake"] = round(float(open_stake or 0.0), 6)
                cur.execute("SELECT count(*), coalesce(sum(close_profit_abs), 0) "
                            "FROM trades WHERE is_open=0 "
                            "AND close_date IS NOT NULL")
                closed, realized_closed = cur.fetchone()
                out["closed_trades"] = int(closed or 0)
                # realized on OPEN trades lives in realized_profit
                # (freqtrade accumulates the partial-exit profit there;
                # close_profit_abs on an open trade is only the LAST
                # exit's chunk); closed trades carry their final figure
                # in close_profit_abs. Summing closed trades only hid
                # every realized partial exit while a grid trade stays
                # open — which for this fleet is nearly always.
                cur.execute("SELECT coalesce(sum(realized_profit), 0) "
                            "FROM trades WHERE is_open=1")
                (realized_open,) = cur.fetchone()
                out["realized"] = round(float(realized_closed or 0.0)
                                        + float(realized_open or 0.0), 6)
                cur.execute("SELECT count(*), max(order_filled_date) FROM orders "
                            "WHERE order_filled_date IS NOT NULL")
                fills, last_fill = cur.fetchone()
                out["fills"] = int(fills or 0)
                out["last_fill_at"] = _iso_db_utc(last_fill)
                cur.execute("SELECT count(*) FROM orders "
                            "WHERE order_filled_date IS NOT NULL "
                            "AND order_filled_date > "
                            "datetime('now', '-1 day')")
                out["fills_24h"] = int(cur.fetchone()[0] or 0)
            finally:
                con.close()
        except Exception:
            pass
    if entry.get("username") and entry.get("password") and entry.get("port"):
        # honest about why live marks are stale: a wedged engine (429
        # storm / warm-up) is in REST back-off, not "down". Cooldown is
        # per-(bot, path) — surface the worst active back-off across the
        # REST paths the stats probe touches (status + balance).
        bot = str(entry.get("bot_code"))
        cd = 0.0
        for p in ((bot, "/api/v1/status"), (bot, "/api/v1/balance")):
            t = _REST_COOLDOWN.get(p, 0)
            if t > cd:
                cd = t
        out["api_backoff"] = bool(cd and cd > time.time())
        status = _engine_rest(entry, "/api/v1/status")
        out["api_ok"] = status is not None
        if isinstance(status, list) and status:
            profits = [t.get("profit_abs") for t in status
                       if isinstance(t, dict)
                       and isinstance(t.get("profit_abs"), (int, float))]
            pcts = [t.get("profit_pct") for t in status
                    if isinstance(t, dict)
                    and isinstance(t.get("profit_pct"), (int, float))]
            if profits:
                out["open_profit_abs"] = round(sum(profits), 6)
            if pcts:
                out["open_profit_pct"] = round(sum(pcts), 4)
            out["open_enter_tag"] = status[0].get("enter_tag")
        bal = _engine_rest(entry, "/api/v1/balance")
        if isinstance(bal, dict) and isinstance(bal.get("total"),
                                                (int, float)):
            out["wallet_total"] = round(float(bal["total"]), 6)
    if entry.get("pid"):
        try:
            started = ps_console_started_at(int(entry["pid"]))
            if started:
                # ps_console_started_at parses local-wallclock lstart as a
                # naive epoch — emit the wallclock ISO (no Z) so the
                # browser's local-time Date.parse keeps relative ages right
                out["started_at"] = datetime.fromtimestamp(
                    started).isoformat()
        except Exception:
            pass
    return out


_GEOM_CACHE: dict = {}          # bot_code -> (expires_at, geometry | None)


def _live_geometry(entry: dict) -> dict | None:
    """The deployed grid channel, recomputed exactly the way GridStrategy
    does it each candle — the same vendored geometry module, the tuned M2
    params, and the engine's last analyzed candle (REST pair_candles).
    GridStrategy never persists geometry, so reading a grid.json off disk
    was a phantom (nothing ever wrote it); this is the real thing. 60s
    TTL cache because the Fleet view polls every 5s."""
    bot = entry.get("bot_code") or "?"
    hit = _GEOM_CACHE.get(bot)
    if hit and hit[0] > time.time():
        return hit[1]
    geom = None
    tuned = _read_json(os.path.join(GRID_HOME, "strategies",
                                    "GridStrategy.json"), {}) or {}
    params = ((tuned.get("params") or {}).get("buy") or {})
    band_atr = float(params.get("band_atr") or 3.0)
    step_factor = float(params.get("step_factor") or 0.5)
    # 2026-09-15 reset: each slot has its own TF (BTC=1m, ETH/SOL=3m,
    # HYPE=5m); the channel preview probes pair_candles at the slot TF
    # so what the dashboard shows matches what the strategy actually
    # sees. Fallback to 1m (the GridStrategy default) when unknown.
    slot_tf = (entry.get("timeframe") or "1m")
    candles = _engine_rest(
        entry, "/api/v1/pair_candles?pair="
        + urllib.parse.quote(str(entry.get("pair") or ""), safe="")
        + f"&timeframe={slot_tf}&limit=1")
    if isinstance(candles, dict):
        cols = candles.get("columns") or []
        rows = candles.get("data") or []
        if cols and rows and "close" in cols and "atr_pct" in cols:
            row = dict(zip(cols, rows[-1]))
            try:
                close = float(row["close"])
                atr_pct = float(row["atr_pct"])
                sys.path.insert(0, os.path.join(GRID_HOME, "execution"))
                from grid_geometry import (  # noqa: E402
                    channel, fee_floor_step, geometric_lines, per_line_size)
                step = fee_floor_step(atr_pct * step_factor, 0.02,
                                      entry.get("venue") or "hyperliquid",
                                      step_min=0.1, step_max=2.0)
                low, high = channel(close, atr_pct, band_atr=band_atr)
                lines = geometric_lines(low, high, step)
                mid = (low + high) / 2.0
                per_line = per_line_size(100.0, len(lines), min_cost=10.0)
                geom = {
                    "low": low, "high": high,
                    "mid": mid,
                    "band_atr": band_atr, "step_factor": step_factor,
                    "atr_pct": atr_pct, "step_pct": step,
                    "grids": len(lines), "close": close,
                    "amount_per_trade": per_line,
                    "distributed_notional": per_line * sum(
                        1 for ln in lines if ln < mid),
                    "computed_at": utcnow(),
                    "source": "live (engine REST pair_candles + tuned "
                              "params — recomputed as GridStrategy does)",
                }
            except Exception:
                geom = None
    _GEOM_CACHE[bot] = (time.time() + 60.0, geom)
    return geom


def _engine_events(limit: int = 40) -> list[dict]:
    """The standalone analog of the retired brain's journal tail: live
    decision-shaped events from every instance's trades DB — grid-line
    fills, trade opens, trade closes. Newest first."""
    evs = []
    for code, entry in (_ft_registry().get("instances") or {}).items():
        if not isinstance(entry, dict):
            continue
        db = os.path.join(entry.get("dir") or "",
                          "tradesv3.dryrun.sqlite")
        if not os.path.isfile(db):
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            cur = con.cursor()
            cur.execute("SELECT open_date, enter_tag, pair, stake_amount "
                        "FROM trades WHERE is_open=1")
            for at, tag, pair, stake in cur.fetchall():
                evs.append({
                    "at": _iso_db_utc(at), "kind": "engine-open",
                    "slot": code, "venue": entry.get("venue"),
                    "symbol": entry.get("symbol"),
                    "msg": f"{code} {pair or '?'} opened "
                           f"{tag or 'entry'} "
                           f"({float(stake or 0):.2f} USDC)"})
            cur.execute("SELECT close_date, exit_reason, pair, "
                        "close_profit_abs FROM trades "
                        "WHERE is_open=0 AND close_date IS NOT NULL")
            for at, reason, pair, profit in cur.fetchall():
                evs.append({
                    "at": _iso_db_utc(at), "kind": "engine-close",
                    "slot": code, "venue": entry.get("venue"),
                    "symbol": entry.get("symbol"),
                    "msg": f"{code} {pair or '?'} closed "
                           f"{reason or '?'} "
                           f"({float(profit or 0):+.4f} USDC)"})
            cur.execute("SELECT order_filled_date, ft_order_tag, "
                        "ft_order_side, ft_pair, cost, average FROM orders "
                        "WHERE order_filled_date IS NOT NULL")
            for at, tag, side, pair, cost, avg in cur.fetchall():
                evs.append({
                    "at": _iso_db_utc(at), "kind": "engine-fill",
                    "slot": code, "venue": entry.get("venue"),
                    "symbol": entry.get("symbol"),
                    "msg": f"{code} {side or '?'}-fill {tag or ''} "
                           f"{float(cost or 0):.2f} USDC @ "
                           f"{float(avg or 0):.1f}"})
            con.close()
        except Exception:
            continue
    evs = [e for e in evs if e.get("at")]
    evs.sort(key=lambda e: e["at"], reverse=True)
    return evs[:max(1, min(limit, 200))]


def _engine_session_payload() -> dict:
    """Live engine session summary for the Run cards tab: what the
    standalone system is actually running right now (the frozen WT-era
    report archive beneath it is labeled by the freshness banner)."""
    instances = []
    for code, entry in sorted(
            (_ft_registry().get("instances") or {}).items()):
        if not isinstance(entry, dict):
            continue
        stats = _engine_instance_stats(entry)
        stats["pair"] = entry.get("pair")
        stats["port"] = entry.get("port")
        # Derive status live — registry.json's "status" is a snapshot from
        # registration time and never updates, so the live signal is:
        #   running   — pid is alive AND the engine REST probe succeeded
        #   starting  — pid is alive but REST hasn't come up yet (warm-up)
        #   down      — pid is gone
        pid_alive = bool(entry.get("pid")) and _pid_alive(entry["pid"])
        stats["status"] = (
            "running" if pid_alive and stats.get("api_ok") else
            "starting" if pid_alive else
            "down"
        )
        stats["channel"] = _live_geometry(entry)
        instances.append(stats)
    return {"at": utcnow(), "instances": instances}


# ── live reliability ledger (M4 toolchain over the fleet's trades DBs) ──

# every fleet instance runs GridStrategy longs — one archetype bucket
FT_ARCHETYPE = "Long Grid / classic LONG"

_REL_CACHE: tuple | None = None


def _reliability_live() -> dict:
    """Live ledger + closed round-trips computed from the fleet's dry-run
    trades DBs via the M4 toolchain (grid.reliability.pairing.from_sqlite
    → grid.reliability.ledger.compute). 60s TTL — /api/overview polls
    every 5s and the pairing pass is not free. Returns
    {"ledger", "trips", "freshness", "error"}; every field degrades
    safely so the Reliability tab renders an honest empty state rather
    than a 500."""
    global _REL_CACHE
    if _REL_CACHE and _REL_CACHE[0] > time.time():
        return _REL_CACHE[1]
    out = {"ledger": {}, "trips": {}, "freshness": None, "error": None}
    try:
        sys.path.insert(0, os.path.dirname(GRID_HOME))   # repo root → grid pkg
        from grid.reliability.pairing import from_sqlite
        from grid.reliability import ledger as m4ledger
    except Exception as exc:
        out["error"] = f"M4 import failed: {str(exc)[:120]}"
    else:
        sources = []
        newest = 0.0
        for code, entry in (_ft_registry().get("instances") or {}).items():
            if not isinstance(entry, dict):
                continue
            db = os.path.join(entry.get("dir") or "",
                              "tradesv3.dryrun.sqlite")
            if not os.path.isfile(db):
                continue
            # SQLite WAL: live writes land in the -wal sidecar and the main
            # DB mtime only advances on checkpoint — freshness must track
            # both or it under-reports a fleet that is actively trading.
            for suffix in ("", "-wal", "-shm"):
                try:
                    newest = max(newest, os.stat(db + suffix).st_mtime)
                except OSError:
                    pass
            try:
                trips, _report = from_sqlite(db, source_id=code)
            except Exception:
                continue
            if trips:
                sources.append({"archetype": FT_ARCHETYPE, "trips": trips})
        out["ledger"] = m4ledger.compute(sources)
        out["trips"] = {s["archetype"]: s["trips"] for s in sources}
        if newest:
            out["freshness"] = {
                "at": newest,
                "at_iso": datetime.fromtimestamp(
                    newest, tz=timezone.utc).isoformat(),
                "age_s": int(time.time() - newest),
                "kind": "engine trades DBs",
                "path": "grid/state/ft_fleet/*/tradesv3.dryrun.sqlite",
                "note": "live — recomputed from the dry-run trades DBs "
                        "as grid trips close (60s cache)",
            }
    _REL_CACHE = (time.time() + 60.0, out)
    return out


def overview_payload() -> dict:
    st = _load_state()
    ok_status, ctl_status = _ctl_cached("/status")
    daemon = daemon_info()
    pb_ok, _pb_body = _http_json(f"{PB_URL}/api/health", 1.5)
    committed = st.get("committed") or {}
    total_committed = sum(v for v in committed.values()
                          if isinstance(v, (int, float)))
    cfg = config_payload()["config"]
    portfolio = cfg.get("portfolio") or {}
    return {
        "at": utcnow(),
        "daemon": daemon,
        "engine": engine_payload(),
        "ft_fleet": ft_fleet_payload(),
        "ctl": {"reachable": ok_status, "status": ctl_status if ok_status else None},
        # readiness takes the standalone probe UNLESS a live ctl plane
        # answered with a real status dict — the retired-ctl error body is
        # a truthy dict and must never masquerade as one (that fusion was
        # how the M3 companion daemon's diagnostics leaked into the strip)
        "readiness": _readiness(ctl_status if ok_status else None),
        # WT-era fleet map retired: the frozen state.json bots/slots would
        # render September-8 skeletons as if live. The board draws from
        # ft_fleet (live instances) instead.
        "bots": [],
        "slots": [],
        "committed_usd": round(total_committed, 2),
        "journal_tail": _engine_events(40),
        "reliability": reliability_payload(),
        "pocketbase": {"up": pb_ok},
        # the screen cache age comes from state["screen_cache"]["at"]
        # (epoch float written by rescreen_cycle at daemon.py:2327-2330),
        # NOT from state["optimizer"] which the original line read — the
        # old key was never written, so this always returned None.
        "config_digest": {
            "total_usd": (portfolio.get("total_usd")),
            "slots_default": portfolio.get("slots_default"),
            "rescreen_minutes": (cfg.get("screen") or {}).get("rescreen_minutes"),
            "watch_interval_s": (cfg.get("watch") or {}).get("interval_s"),
            "take_profit_pct": (cfg.get("grid_defaults") or {}).get("take_profit_pct"),
        },
    }


def _readiness(ctl_status: dict | None) -> dict:
    """Derived dependency-readiness + capacity facts for the console.

    Surfaces:
      • In a WT-era checkout with a reachable daemon ctl — the daemon's own
        `/status` diagnostics (LLM-provider env, browser CDP, PocketBase,
        venue capacity, connected profiles, bot-type limits).
      • In the standalone workspace (no daemon ctl reachable, freqtrade
        engine in charge) — a truthful probe of the things an operator
        actually wants to glance at: live freqtrade instances (one cell per
        instance with /api/v1/ping status), the .venv-ft binary presence,
        the strategy files, PocketBase, and LLM env keys (kept for the
        market-brief / NLG hint).

    Returns `{}` when nothing is reachable AND no signal is available —
    the frontend handles that as a graceful "diagnostics offline" state.
    """
    if isinstance(ctl_status, dict) and ctl_status:
        # WT-era daemon ctl reachable — keep the legacy shape so any
        # legacy UI copy stays parseable (we never expect this branch in
        # this workspace, but it's a cheap future-compat path).
        env = ctl_status.get("env") or {}
        llm_env = env.get("llm_env") or {}
        capacity = ctl_status.get("capacity") or {}
        max_active = capacity.get("max_active") or {}
        active = capacity.get("active") or {}
        other_max = _num(max_active.get("other"))
        other_active = _num(active.get("other"))
        premium_max = _num(max_active.get("premium"))
        premium_active = 0
        if isinstance(active.get("premium"), dict):
            premium_active = sum(_num(v) for v in active["premium"].values())
        profiles = ctl_status.get("profiles") or []
        real_profiles = [p for p in profiles if isinstance(p, dict)
                         and p.get("paperTrading") is False]
        return {
            "reachable": True, "source": "daemon-ctl",
            "llm_env": {k: bool(v) for k, v in llm_env.items()} if isinstance(llm_env, dict) else {},
            "browser_cdp": bool(env.get("browser_cdp")),
            "pb_env": bool(env.get("pb_env")),
            "pocketbase": bool(env.get("pb_env")),
            "capabilities": ctl_status.get("capabilities") or {},
            "capacity": {
                "other": {"active": other_active, "max": other_max},
                "premium": {"active": premium_active, "max": premium_max},
            },
            "profiles": profiles,
            "real_profiles": real_profiles,
            "account_limits": ctl_status.get("account_limits") or {},
        }

    # Standalone workspace probe — read from grid/dev-managed state.
    fleet = _ft_fleet_readiness()
    return {
        "reachable": True, "source": "grid-dev",
        "ft_fleet": fleet,
        "venv_ft": os.path.isfile(
            os.path.join(os.path.dirname(STATE_DIR), "..", ".venv-ft", "bin", "freqtrade")),
        "strategy_files": _strategy_files_present(),
        # PocketBase sidecar health via PB_URL (grid/dev exports :8290 —
        # the workspace PB; 8090 belongs to the M3 stack and is never
        # probed from here)
        "pocketbase": _http_json(f"{PB_URL}/api/health", 1.5)[0],
        "llm_env": _llm_env_keys(),
    }


def _strategy_files_present() -> bool:
    """The GridStrategy files this workspace's instances need to boot."""
    sdir = os.path.join(os.path.dirname(STATE_DIR), "strategies")
    for fn in ("GridStrategy.py", "GridStrategy.json", "grid_geometry.py"):
        if not os.path.isfile(os.path.join(sdir, fn)):
            return False
    return True


def _llm_env_keys() -> dict:
    """Bool map of which LLM provider keys are present in state/llm.env or
    process env. Same shape as the legacy daemon shape the readiness strip
    used to render.

    Reuses _llm_sidecar() — the sidecar file is written in `export KEY=…`
    form, and this probe used to split raw lines on "=" itself, leaving the
    `export ` prefix on every key so ALL cells read "down" while the live
    provider pings succeeded. One parser, no drift.
    """
    candidates = ("MISTRAL_API_KEY", "OPENROUTER_API_KEY", "NVIDIA_API_KEY",
                  "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
    side = _llm_sidecar()
    out = {}
    for k in candidates:
        v = side.get(k) or os.environ.get(k) or ""
        out[k] = bool(v and v not in ("", "changeme", "your-key"))
    return out


def _optimizer_standalone_payload() -> dict:
    """Truthful payload for the Optimizer tab when the WT-era slow-loop
    optimizer doesn't apply (it ran against WunderTrading's grid_bots and
    has no freqtrade analog — GridStrategy handles per-candle grid
    revaluation in-strategy).

    What an operator wants to see here: the live per-instance grid
    geometry (ATR channel, step, levels), tuning history from
    GridStrategy.json's Hyperopt v1 params, the reliability ledger
    summary, and the recent decision journal tail.
    """
    # Per-instance LIVE geometry: GridStrategy computes the channel
    # in-strategy each candle and never persists it — the old grid.json
    # read was a phantom (nothing ever wrote it, so the table always
    # rendered empty). Recompute it the same way the strategy does:
    # the vendored geometry module + the tuned M2 params + the engine's
    # last analyzed candle via REST pair_candles.
    geom = []
    for code, entry in sorted(
            (_ft_registry().get("instances") or {}).items()):
        if not isinstance(entry, dict):
            continue
        g = _live_geometry(entry)
        if not g:
            continue
        geom.append({
            "bot_code": code,
            "pair": entry.get("pair"),
            "mid": g["mid"],
            "low": g["low"],
            "high": g["high"],
            "band_atr": g["band_atr"],
            "atr_pct_derived": g["atr_pct"],
            "atr_pct": g["atr_pct"],
            "step_pct": g["step_pct"],
            "grids": g["grids"],
            "amount_per_trade": g["amount_per_trade"],
            "distributed_notional": g["distributed_notional"],
            "close": g["close"],
            "computed_at": g["computed_at"],
            "derived_from": g["source"],
        })

    # Tuned params (M2 hyperopt v1) — read from grid/strategies.
    tuned = _read_json(os.path.join(os.path.dirname(STATE_DIR), "strategies",
                                    "GridStrategy.json"), {}) or {}

    # Reliability summary — live from the fleet's trades DBs (M4 math),
    # falling back to the WT-era file ledger only while it still exists.
    # The source is declared so the panel can label the numbers honestly:
    # an empty live ledger silently showing the frozen file was read as
    # "live context" by the Optimizer tab.
    live_rel = _reliability_live().get("ledger")
    rel = (live_rel
           or _read_json(os.path.join(STATE_DIR, "reliability.json"), {})
           or {})
    rel_source = ("engine trades DBs (live)" if live_rel
                  else "state/reliability.json (frozen WT-era file)")

    # Decision journal tail (state/decisions.jsonl) — last 5 entries.
    decisions = []
    dpath = os.path.join(STATE_DIR, "decisions.jsonl")
    try:
        with open(dpath) as f:
            lines = f.readlines()[-5:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                decisions.append(json.loads(line))
            except Exception:
                pass
    except OSError:
        pass

    return {
        "geom": geom,
        "tuned_params": tuned,
        "reliability": rel,
        "reliability_source": rel_source,
        "decisions_tail": decisions,
        "note": "GridStrategy handles per-candle grid revaluation in-strategy; "
                "geom[] is recomputed live from the engine's last analyzed "
                "candle (GridStrategy never persists geometry). tuned_params "
                "is the M2-hyperopt set in effect; reliability[] is the M4 "
                "ledger; decisions_tail is the frozen WT-era journal."
    }


def _ft_fleet_readiness() -> list:
    """Per-instance readiness probes — what the daemon used to call
    "capacity / profiles / browser_cdp" comes out as instance-level rows
    for the freqtrade-dry-run backend.
    """
    out = []
    fleet_root = os.path.join(STATE_DIR, "ft_fleet", "registry.json")
    try:
        reg = _read_json(fleet_root, {}) or {}
    except Exception:
        reg = {}
    instances = (reg.get("instances") or {}) if isinstance(reg, dict) else {}
    for bot_code, entry in instances.items():
        if not isinstance(entry, dict):
            continue
        port = entry.get("port")
        api_ok = False
        if port:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{int(port)}/api/v1/ping",
                    timeout=1.5) as r:
                    api_ok = (r.status == 200)
            except Exception:
                api_ok = False
        out.append({
            "bot_code": bot_code,
            "status": entry.get("status"),
            "pair": entry.get("pair"),
            "port": port,
            "api_ok": api_ok,
            "backend": entry.get("backend"),
        })
    return out


def _freshness(path: str, kind: str) -> dict:
    """Per-tab freshness block — when the underlying state file was last
    written and how stale it is. Tabs that show WT-era data files
    (decisions journal, reports, reliability ledger) get a top banner
    explaining that the brain is closed and the data is a frozen snapshot
    from 2026-09-14. `frozen`/`frozen_at` carry the server-side pivot
    verdict (state/engine.json since) so the UI doesn't have to hardcode
    the epoch. The JS reads `freshness.at` + `freshness.note` and renders
    the banner.
    """
    frozen_at = (engine_payload() or {}).get("since")
    try:
        st = os.stat(path)
        at = st.st_mtime
    except OSError:
        return {"at": None, "age_s": None, "path": path, "kind": kind,
                "frozen": True, "frozen_at": frozen_at,
                "note": f"{kind} state file does not exist — the WT-era "
                        "brain is closed in this workspace; data shown "
                        "(if any) is a snapshot from before the pivot."}
    age_s = int(time.time() - at)
    at_iso = datetime.fromtimestamp(at, tz=timezone.utc).isoformat()
    frozen = bool(frozen_at and at_iso[:19] < str(frozen_at)[:19])
    return {"at": at, "at_iso": at_iso,
            "age_s": age_s, "path": path, "kind": kind,
            "frozen": frozen, "frozen_at": frozen_at,
            "note": f"{kind} state file last written {age_s}s ago — if "
                    "this predates the WT-era pivot (2026-09-14), the "
                    "brain is closed in this workspace and no new "
                    "entries will appear until M3 lands."}


def _num(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# ── daemon ops ─────────────────────────────────────────────────────────

def _wait_gone(pid, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.4)
    return False


def daemon_stop(force=False) -> tuple[int, dict]:
    """Stop the workspace's standalone runtime via `grid/dev stop`.

    Fire-and-forget so the JSON response can be flushed before `grid/dev`
    SIGTERMs this console (a synchronous run races the response — the test
    curl gets "Empty reply from server"). If `force` is set, fall back to a
    direct SIGKILL on the live :8798 listener.
    """
    pid = _dev_console_pid()
    if pid is None:
        return 409, {"error": "mission not running"}
    log = open(os.path.join(STATE_DIR, "logs", "grid-dev-stop.log"), "ab")
    try:
        subprocess.Popen([sys.executable, _dev_script(), "stop"],
                         cwd=GRID_HOME, stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as exc:
        return 500, {"error": f"grid/dev stop failed: {exc}"}
    if force:
        time.sleep(0.5)  # give grid/dev a head start
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return 200, {"stopping": True, "pid": pid,
                 "supervisor": "grid/dev",
                 "killed_forcefully": bool(force)}


def _dev_script() -> str:
    """The workspace's `grid/dev` supervisor (standalone runtime) — the brain
    lifecycle lives here in this checkout, not in grid-autonomy's scripts/.
    The legacy daemon_start() wrapper around scripts/start.sh was a hard 500
    after the WT engine was retired; this is the wired replacement."""
    return os.path.join(GRID_HOME, "dev")


def _port_listen_pid(port: int) -> int | None:
    """PID of whatever is LISTENing on `port` (loopback only), or None.
    Best-effort; avoids adopting a foreign listener."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if line.startswith("p"):
            try:
                return int(line[1:])
            except ValueError:
                pass
    return None


def _dev_status_lines() -> list[str]:
    """Run `grid/dev status` and return its lines (used as a readiness probe
    that doesn't require a separate pid file: we just check whether the
    console is 'RUNNING' on :8798, which is what the UI advertises)."""
    try:
        return subprocess.run(
            [sys.executable, _dev_script(), "status"],
            capture_output=True, text=True, timeout=5,
        ).stdout.splitlines()
    except Exception:
        return []


def _dev_console_pid() -> int | None:
    """Parse `grid/dev status` for the console pid. Falls back to reading the
    live :8798 listener if status output is unparseable."""
    for line in _dev_status_lines():
        # grid/dev renders the console line as:
        #   "console         : RUNNING (pid 65089) · http://127.0.0.1:8798/#fleet"
        m = re.search(r"console\b[^:\n]*:\s*RUNNING\s*\(pid\s+(\d+)\)", line)
        if m:
            return int(m.group(1))
    return _port_listen_pid(CONSOLE_PORT)


def daemon_start(live_paper=False, clear_kill=False) -> tuple[int, dict]:
    """Start the workspace's standalone runtime via `grid/dev start`.

    Fire-and-forget: the API returns immediately and the console boot
    happens off-thread. Status bar refreshes within a few seconds.

    The legacy grid-autonomy daemon (the WT-era brain) was retired with the
    WunderTrading engine; the standalone mission brain for this checkout is
    `grid/dev`, which supervises the console + dry-run freqtrade engine.
    `live_paper` is accepted for API compatibility but ignored — this
    workspace is DRY-RUN only (see state/engine.json).
    """
    dev = _dev_script()
    if not os.path.isfile(dev):
        return 500, {"error": f"grid/dev not found: {dev}",
                     "hint": "grid/dev is the supervisor for this workspace"}
    if _dev_console_pid() is not None:
        return 409, {"error": "mission already running",
                     "running": True, "supervisor": "grid/dev"}
    log_dir = os.path.join(STATE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log = open(os.path.join(log_dir, "grid-dev.log"), "ab")
    try:
        subprocess.Popen([sys.executable, dev, "start"],
                         cwd=GRID_HOME, stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as exc:
        return 500, {"error": f"grid/dev start failed: {exc}"}
    return 200, {"starting": True, "supervisor": "grid/dev",
                 "engine": "freqtrade", "mode": "dry-run",
                 "note": "grid/dev start fired in the background; "
                         "status bar will refresh within ~5s"}


def daemon_restart(clear_kill=False, live_paper=None) -> tuple[int, dict]:
    """Restart the workspace's standalone runtime (delegated to `grid/dev`).

    Fire-and-forget: do NOT block the HTTP response on grid/dev's startup
    (a synchronous run races the SIGTERM that grid/dev issues against our
    own console — the previous XHR got '52 Empty reply from server'). The
    status bar will reflect the new state within a few seconds.

    `live_paper` and `clear_kill` are accepted for API compatibility with
    the legacy WT-era restart endpoint; both are ignored — this workspace
    is DRY-RUN only and `grid/dev` does not use a KILL file.
    """
    log = open(os.path.join(STATE_DIR, "logs", "grid-dev-restart.log"), "ab")
    try:
        subprocess.Popen([sys.executable, _dev_script(), "restart"],
                         cwd=GRID_HOME, stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as exc:
        return 500, {"error": f"grid/dev restart failed: {exc}"}
    return 200, {"restarting": True, "supervisor": "grid/dev",
                 "note": "grid/dev restart fired in the background; "
                         "status bar will refresh within ~5s"}


# ── HTTP handler ───────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "grid-console/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing --
    def _json(self, code, obj):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True  # non-browser client (curl, tests)
        host = self.headers.get("Host", "")
        try:
            from urllib.parse import urlparse
            o = urlparse(origin)
            return o.netloc == host or o.hostname in ("127.0.0.1", "localhost")
        except Exception:
            return False

    def log_message(self, *a):
        pass

    # -- static --
    def _static(self, path):
        if path == "/":
            path = "/index.html"
        rel = os.path.normpath(path.lstrip("/"))
        if rel.startswith("..") or os.path.isabs(rel):
            self._json(404, {"error": "not found"})
            return
        full = os.path.join(STATIC_DIR, rel)
        if not os.path.isfile(full):
            self._json(404, {"error": "not found"})
            return
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- routing --
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        q = parse_qs(u.query)
        route = u.path

        def q1(name, default):
            return (q.get(name) or [default])[0]

        if route == "/" or not route.startswith("/api/"):
            self._static(route)
        elif route == "/api/overview":
            self._json(200, overview_payload())
        elif route == "/api/daemon":
            self._json(200, daemon_info())
        elif route == "/api/state":
            self._json(200, _load_state())
        elif route == "/api/journal":
            st = _load_state()
            limit = int(q1("limit", 80))
            self._json(200, {"journal": (st.get("journal") or [])[-limit:]})
        elif route == "/api/decisions":
            self._json(200, {"decisions":
                             decisions_payload(int(q1("limit", 100))),
                             "freshness": _freshness(
                                 os.path.join(STATE_DIR, "decisions.jsonl"),
                                 "decisions journal"),
                             # live standalone decision stream: grid fills
                             # + trade opens/closes from the trades DBs —
                             # the frozen WT journal above is labeled by
                             # the freshness banner
                             "engine_events": _engine_events(60)})
        elif route.startswith("/api/decisions/"):
            did = os.path.basename(route[len("/api/decisions/"):])
            payload = decision_payload_by_id(did)
            if payload is None:
                self._json(404, {"error": f"no decision {did}"})
            else:
                self._json(200, payload)
        elif route == "/api/reliability":
            self._json(200, reliability_payload())
        elif route == "/api/reliability/archive":
            self._json(200, reliability_archive_payload(
                int(q1("limit", 20))))
        elif route == "/api/recommendations":
            self._json(200, recommendations_payload(
                int(q1("limit", 100))))
        elif route == "/api/screen":
            self._json(200, {"screen": screen_payload()})
        elif route == "/api/optimizer":
            # The WT-era daemon ran a slow-loop position optimizer that
            # edited WunderTrading grids directly. In this workspace the
            # execution engine is freqtrade-dry-run — the strategy (GridStrategy)
            # is what adjusts the grid per candle, no separate optimizer chain
            # applies. Return a "not applicable" payload so the Optimizer
            # tab renders an honest explanation instead of an error toast.
            # (No ctl round-trip: the ctl plane is retired here — see
            # CTL_RETIRED — and the optimizer payload is built entirely from
            # workspace artifacts.)
            self._json(200, {"optimizer": None,
                             "applicable": False,
                             "engine": "freqtrade",
                             "reason": "execution engine is freqtrade-dry-run; "
                                       "GridStrategy handles per-candle grid "
                                       "adjustments in-strategy — no separate "
                                       "slow-loop optimizer applies",
                             "fast": _optimizer_standalone_payload()})
        elif route == "/api/optimizer/swap-log":
            # swap_log + per-slot idle trackers + last arbiter verdict
            # from state.json's optimizer block (the daemon already keeps
            # the live copy — no extra ctl call needed)
            self._json(200, optimizer_swap_log())
        elif route == "/api/llm/health":
            # live provider ping + role routing matrix; 60s in-process cache
            self._json(200, llm_health())
        elif route == "/api/observe":
            ok, body = _ctl_cached("/observe")
            # fail-soft: degrade with a 200 + {"error": ...} so the UI can
            # keep rendering last-persisted state when the daemon is down
            self._json(200, body if ok else
                       {"error": _ctl_err(body), "detail": body})
        elif route == "/api/status":
            ok, body = _ctl_cached("/status")
            self._json(200, body if ok else
                       {"error": _ctl_err(body), "detail": body})
        elif route == "/api/pnl":
            self._json(200, pnl_payload())
        elif route == "/api/chart":
            code, payload = _chart_bars(q1("venue", ""), q1("symbol", ""),
                                        q1("interval", "5m"),
                                        q1("bars", "96"))
            self._json(code, payload)
        elif route == "/api/position-sweeps":
            self._json(200, {"sweeps": position_sweeps_payload(
                int(q1("limit", 25)))})
        elif route == "/api/reports":
            self._json(200, {"reports": reports_index(),
                             "freshness": _freshness(
                                 os.path.join(STATE_DIR, "reports"),
                                 "reports index"),
                             # live session rows rendered above the frozen
                             # WT-era archive
                             "engine": _engine_session_payload()})
        elif route.startswith("/api/reports/"):
            stem = os.path.basename(route[len("/api/reports/"):])
            rdir = os.path.join(STATE_DIR, "reports")
            jpath, mpath = os.path.join(rdir, stem + ".json"), \
                os.path.join(rdir, stem + ".md")
            if not (os.path.isfile(jpath) or os.path.isfile(mpath)):
                self._json(404, {"error": "no such run card"})
                return
            md = None
            if os.path.isfile(mpath):
                with open(mpath, encoding="utf-8", errors="replace") as f:
                    md = f.read()
            self._json(200, {"stem": stem, "json": _read_json(jpath), "md": md})
        elif route == "/api/logs":
            self._json(200, logs_payload(int(q1("lines", 300)), q1("grep", None)))
        elif route == "/api/config":
            self._json(200, config_payload())
        elif route == "/api/llm":
            self._json(200, llm_payload())
        elif route == "/api/meta":
            pid = _dev_console_pid()
            self._json(200, {
                "console_port": CONSOLE_PORT,
                "ctl_port": _ctl_port(),  # legacy field; the ctl plane is absent
                                        # in this workspace (no brain here)
                "supervisor": "grid/dev" if pid else "none",
                "supervisor_script": _dev_script(),
                "pocketbase": PB_URL, "state_dir": STATE_DIR,
                "grid_home": GRID_HOME,
                "launchd_label": LAUNCHD_LABEL,  # absent here; kept for compat
                "pid": os.getpid(), "started": getattr(SERVER, "started", None),
                "wt_account": WT_ACCOUNT_LABEL,
                "engine": engine_payload(),
            })
        else:
            self._json(404, {"error": "unknown path"})

    def do_POST(self):
        from urllib.parse import urlparse
        route = urlparse(self.path).path
        if not self._same_origin():
            self._json(403, {"error": "cross-origin refused"})
            return
        if not route.startswith("/api/"):
            self._json(404, {"error": "unknown path"})
            return
        body = self._body()
        confirmed = bool(body.get("confirm"))

        if route == "/api/ctl/rescreen":
            ok, resp = _ctl("/rescreen", "POST")
            self._json(200 if ok else 502, resp if ok else
                       {"error": _ctl_err(resp), "detail": resp})
        elif route == "/api/ctl/optimize":
            ok, resp = _ctl("/optimize", "POST")
            self._json(200 if ok else 502, resp if ok else
                       {"error": _ctl_err(resp), "detail": resp})
        elif route == "/api/ctl/reliability":
            ok, resp = _ctl("/reliability", "POST")
            self._json(200 if ok else 502, resp if ok else
                       {"error": _ctl_err(resp), "detail": resp})
        elif route == "/api/ctl/rotate":
            slot = body.get("slot")
            if slot is None:
                self._json(400, {"error": "missing slot"})
                return
            ok, resp = _ctl("/rotate", "POST", {"slot": slot})
            self._json(200 if ok else 502, resp if ok else
                       {"error": _ctl_err(resp), "detail": resp})
        elif route == "/api/ctl/kill":
            if not confirmed:
                self._json(400, {"error": 'pass {"confirm": true}'})
                return
            ok, resp = _ctl("/kill", "POST")
            if not ok:
                try:
                    open(KILL_FILE, "w").write(utcnow())
                    resp = {"killed": True, "via": "direct"}
                except Exception as exc:
                    self._json(502, {"error": str(exc)})
                    return
            self._json(200, resp)
        elif route == "/api/ctl/unkill":
            if not confirmed:
                self._json(400, {"error": 'pass {"confirm": true}'})
                return
            if os.path.exists(KILL_FILE):
                try:
                    os.remove(KILL_FILE)
                except OSError as exc:
                    self._json(500, {"error": str(exc)})
                    return
            self._json(200, {"kill_file": False})
        elif route == "/api/config":
            code, resp = apply_config_edits(body.get("edits"))
            self._json(code, resp)
        elif route == "/api/llm":
            code, resp = apply_llm(body)
            self._json(code, resp)
        elif route == "/api/llm/validate":
            code, resp = validate_llm()
            self._json(code, resp)
        elif route == "/api/daemon/stop":
            if not confirmed:
                self._json(400, {"error": 'pass {"confirm": true}'})
                return
            self._json(*daemon_stop(force=bool(body.get("force"))))
        elif route == "/api/daemon/start":
            if not confirmed:
                self._json(400, {"error": 'pass {"confirm": true}'})
                return
            self._json(*daemon_start(live_paper=bool(body.get("live_paper")),
                                     clear_kill=bool(body.get("clear_kill"))))
        elif route == "/api/daemon/restart":
            if not confirmed:
                self._json(400, {"error": 'pass {"confirm": true}'})
                return
            # live_paper: True/False forces the mode; null/omitted preserves
            # the current mode on manual restarts (launchd always uses
            # --live-paper regardless of this flag).
            self._json(*daemon_restart(
                clear_kill=bool(body.get("clear_kill")),
                live_paper=body.get("live_paper")))
        elif route in ("/api/dev/reset", "/api/dev/reset-wt", "/api/dev/clean"):
            if not confirmed:
                self._json(400, {"error": 'pass {"confirm": true}'})
                return
            self._json(*dev_action(route.rsplit("/", 1)[1], body))
        else:
            self._json(404, {"error": "unknown path"})


# ── dev-script actions ─────────────────────────────────────────────────

DEV_SCRIPT = os.path.join(GRID_HOME, "dev")
DEV_LOG = os.path.join(STATE_DIR, "logs", "dev.log")


def dev_action(action: str, body: dict) -> tuple[int, dict]:
    """Run a `dev <action>` through the single dev script, detached.

    reset/reset-wt stop the daemon AND this console (the dev script
    supervises the whole stack), so the command must run detached —
    its output lands in state/logs/dev.log and the frontend reloads
    once the console is back. `clean` does not stop the console, but
    uses the same path for uniformity.
    """
    if not os.path.isfile(DEV_SCRIPT):
        return 500, {"error": f"dev script not found: {DEV_SCRIPT}"}
    args = [DEV_SCRIPT, action, "--yes"]
    if action == "reset":
        if body.get("keep_decisions"):
            args.append("--keep-decisions")
        if body.get("start"):
            args.append("--start")
        # the WT reset is explicit, never defaulted: a local-only reset
        # leaves the paper bots running (they get re-adopted or block
        # redeploys), a --wt reset deletes them outright
        args.append("--wt" if body.get("wt") else "--no-wt")
    os.makedirs(os.path.dirname(DEV_LOG), exist_ok=True)
    try:
        with open(DEV_LOG, "ab") as log:
            subprocess.Popen(args, cwd=GRID_HOME, stdout=log, stderr=log,
                             stdin=subprocess.DEVNULL,
                             start_new_session=True)
    except Exception as exc:  # noqa: BLE001
        return 500, {"error": f"spawn failed: {exc}"}
    return 200, {"started": True, "action": action, "args": args,
                 "log": "state/logs/dev.log"}


SERVER = None


def _ledger_sync_loop():
    """Own the M4 reliability-ledger cadence in-process.

    The com.tvcli.grid-ledger-sync LaunchAgent (6h StartInterval) is the
    original seam, but on this external-volume setup launchd spawns it
    straight into exit 78 / EX_CONFIG with no output (the agent context
    cannot open the declared stdio paths on /Volumes), which left
    state/reliability.json stale for the whole trading day while the
    manual code path runs clean. This loop runs the SAME code path
    (`grid/dev ledger-sync`, idempotent, atomic reliability.json write)
    from the console's own process context every 30 minutes. The
    LaunchAgent stays installed as a harmless belt-and-braces; both
    paths recompute from the engine DBs so interleaved runs converge."""
    time.sleep(60)
    while True:
        log = os.path.join(STATE_DIR, "logs", "ledger-sync.log")
        try:
            os.makedirs(os.path.dirname(log), exist_ok=True)
            r = subprocess.run(
                [sys.executable, os.path.join(GRID_HOME, "dev"),
                 "ledger-sync"],
                cwd=GRID_HOME, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=180, text=True)
            with open(log, "a") as fh:
                stamp = datetime.now().astimezone().isoformat(
                    timespec="seconds")
                fh.write(f"[{stamp}] ledger-sync exit={r.returncode}\n")
                fh.write(r.stdout or "")
        except Exception as exc:  # noqa: BLE001
            try:
                with open(log, "a") as fh:
                    fh.write(f"[{datetime.now().astimezone().isoformat(
                        timespec='seconds')}] ledger-sync loop error: "
                        f"{exc}\n")
            except OSError:
                pass
        time.sleep(1800)


def main():
    global SERVER
    # Graceful SIGTERM: exit 0 so a supervisor (launchd KeepAlive with
    # SuccessfulExit=false) does not treat a stop as a crash and restart it.
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # Bind host is env-overridable for containers (docker -p needs 0.0.0.0
    # inside the container; the local default stays loopback-only).
    _bind_host = os.environ.get("GRID_BIND_HOST", "127.0.0.1")
    srv = ThreadingHTTPServer((_bind_host, CONSOLE_PORT), Handler)
    SERVER = srv
    srv.started = utcnow()
    import threading
    threading.Thread(target=_ledger_sync_loop, daemon=True,
                     name="ledger-sync").start()
    print(f"grid-autonomy console on http://{_bind_host}:{CONSOLE_PORT} "
          f"(ctl :{_ctl_port()}, state {STATE_DIR})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
