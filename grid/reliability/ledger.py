#!/usr/bin/env python3
"""Reliability ledger — per-archetype closed round-trip stats (M4).

Rebuild of the WunderTrading positions-history ledger (frozen reference:
wt_reference.py, from grid-autonomy execution/reliability_grid.py) on
freqtrade-native evidence: per-level grid round-trips paired from
Order.ft_order_tag (pairing.py).

State file (default grid/state/reliability.json) is SHAPE-COMPATIBLE with
the WT ledger the daemon already consumes:

    {"<archetype>": {"samples": n, "synthetic_samples": m,
                     "profit_factor": p, "recent_pf": r, "win_rate": w,
                     "expectancy_usd": e, "max_dd_usd": d,
                     "gross_profit_usd": gp, "gross_loss_usd": gl}}

plus additive grid-native fields (tp_trips, full_exit_trips,
avg_trip_gain_pct, updated_at) — the daemon's size_multiplier ladder and
refuse_new_archetype kill gate read this unchanged.

Semantics preserved from the WT reference (verified against its source):
  - REAL samples gate the ladder; synthetic (backtest seed) rows are
    counted separately and NEVER influence PF / win rate / expectancy.
  - recent_pf over the last RECENT_WINDOW (20) closed trips; PF capped at
    99.0 when there are no losses.
  - zero-sample updates never erase an existing bucket.
  - rotated-out instances archive their trips per archetype (bounded,
    deduped by (strategy_id, close_ts)) so history survives rotation.

Gates (mirroring daemon.py, local so the standalone repo can test them):
  size_multiplier:      base 25% → probe (samples >= 10) → full
                        (samples >= 30 and PF >= 1.3)
  refuse_new_archetype: samples >= kill_min_samples (10) and recent_pf < 1.0

CLI (see _cli): ingest sqlite / backtest-zip sources into the ledger:
    python3 -m grid.reliability.ledger --backtest-zip <zip>=<archetype> \
        --sqlite <tradesv3.sqlite>=<archetype> [--out PATH] [--report]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):  # direct run: python grid/reliability/ledger.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from grid.reliability.pairing import (  # noqa: E402
        from_backtest_zip, from_sqlite)
    from grid.reliability import wt_reference  # noqa: E402
else:
    from .pairing import from_backtest_zip, from_sqlite  # noqa: E402
    from . import wt_reference  # noqa: E402

PROFIT_FACTOR_CAP = 99.0
RECENT_WINDOW = 20

# Canonical reliability-ledger keys — identical table to wt_reference
# (keep in sync with universe_screen.ARCHETYPE in grid-autonomy).
ARCHETYPE_LABELS = wt_reference.ARCHETYPE_LABELS

DEFAULT_STATE_DIR = Path(__file__).resolve().parents[1] / "state"
DEFAULT_LEDGER = DEFAULT_STATE_DIR / "reliability.json"
DEFAULT_ARCHIVE = DEFAULT_STATE_DIR / "reliability_archive.json"
ARCHIVE_MAX_PER_ARCHETYPE = 500

# escalation ladder + kill gate defaults (daemon.py / config.yaml autonomy)
LADDER_BASE_PCT = 0.25
LADDER_PROBE_PCT = 0.40
LADDER_FULL_PCT = 0.50
LADDER_PROBE_MIN_SAMPLES = 10
LADDER_FULL_MIN_SAMPLES = 30
LADDER_FULL_MIN_PF = 1.3
KILL_MIN_SAMPLES = 10

_SOURCE_SPEC_RE = re.compile(r"^(?P<path>.+?)(?:=(?P<archetype>.+))?$")


# ── ledger keys ────────────────────────────────────────────────────────

def ledger_key(archetype):
    """Canonical ledger key (wt_reference.ledger_key semantics)."""
    return wt_reference.ledger_key(archetype)


# ── per-archetype stats ─────────────────────────────────────────────────

def stats(trips):
    """Stats over trips (closed round-trips) — wt_reference._stats shape.

    REAL trips (synthetic falsy) drive every metric; synthetic rows are
    counted separately. Additive grid-native fields: tp_trips,
    full_exit_trips, avg_trip_gain_pct (fee-less price ratio over tp
    trips with a gain), updated_at.
    """
    trips = [t for t in (trips or []) if isinstance(t, dict)]
    real = [t for t in trips if not t.get("synthetic")]
    real.sort(key=lambda t: _num(t.get("close_ts")))  # chronological: the
    # recent-PF window must not depend on caller ordering
    synthetic_samples = len(trips) - len(real)
    samples = len(real)
    pnls = [_num(t.get("pnl_usd")) for t in real]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_loss > 0:
        profit_factor = round(gross_profit / gross_loss, 4)
    else:
        profit_factor = round(PROFIT_FACTOR_CAP if gross_profit > 0 else 0.0, 4)

    recent = pnls[-RECENT_WINDOW:]
    rw = [p for p in recent if p > 0]
    rl = [p for p in recent if p < 0]
    rgp, rgl = sum(rw), abs(sum(rl))
    if rgl > 0:
        recent_pf = round(rgp / rgl, 4)
    else:
        recent_pf = round(PROFIT_FACTOR_CAP if rgp > 0 else 0.0, 4)

    peak = cum = max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    gains = [t["gain_pct"] for t in real
             if t.get("kind") == "tp" and t.get("gain_pct") is not None]
    out = {
        "samples": samples,
        "synthetic_samples": synthetic_samples,
        "profit_factor": profit_factor,
        "recent_pf": recent_pf,
        "win_rate": round(len(wins) / samples, 4) if samples else 0.0,
        "expectancy_usd": round(sum(pnls) / samples, 4) if samples else 0.0,
        "max_dd_usd": round(max_dd, 4),
        "gross_profit_usd": round(gross_profit, 4),
        "gross_loss_usd": round(gross_loss, 4),
        "tp_trips": sum(1 for t in real if t.get("kind") == "tp"),
        "full_exit_trips": sum(1 for t in real if t.get("kind") == "full_exit"),
        "updated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
    }
    if gains:
        out["avg_trip_gain_pct"] = round(sum(gains) / len(gains), 6)
    return out


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def update(ledger, archetype, trips):
    """Recompute one archetype bucket in `ledger` (dict mutated).

    Zero REAL+synthetic trips → existing bucket kept untouched (the WT
    no-erase rule: a quiet cycle must not wipe an archetype's history).
    """
    key = ledger_key(archetype)
    trips = [t for t in (trips or []) if isinstance(t, dict)]
    if not trips and key in ledger:
        return ledger
    ledger[key] = stats(trips)
    return ledger


def compute(sources):
    """[{archetype, trips}] → a fresh ledger dict (one bucket per key)."""
    ledger: dict = {}
    for src in sources or []:
        update(ledger, src.get("archetype") or "unknown",
               src.get("trips"))
    return ledger


# ── ladder + kill gates (daemon.py semantics, standalone) ──────────────

def _bucket(reliability, archetype):
    """(key, stats) — flat single-bucket fallback like daemon.py."""
    if not isinstance(reliability, dict):
        return None, {}
    key = ledger_key(archetype)
    st = reliability.get(key)
    if isinstance(st, dict):
        return key, st
    if "samples" in reliability or "profit_factor" in reliability:
        return key, reliability
    return key, {}


def size_multiplier(reliability, archetype, cfg=None):
    """(multiplier, tier, stats) — the escalation ladder.

    Unproven archetypes start at base (25% target of slot); probe from
    LADDER_PROBE_MIN_SAMPLES real trips; full only at >= 30 samples AND
    PF >= 1.3 (daemon.py size_multiplier).
    """
    cfg = cfg or {}
    base = _num(cfg.get("base_pct"), LADDER_BASE_PCT)
    probe = _num(cfg.get("probe_pct"), LADDER_PROBE_PCT)
    full = _num(cfg.get("full_pct"), LADDER_FULL_PCT)
    _, st = _bucket(reliability, archetype)
    samples = int(_num(st.get("samples")))
    pf = _num(st.get("profit_factor"))
    if samples >= LADDER_FULL_MIN_SAMPLES and pf >= LADDER_FULL_MIN_PF:
        return full, "full", st
    if samples >= LADDER_PROBE_MIN_SAMPLES:
        return probe, "probe", st
    return base, "base", st


def refuse_new_archetype(reliability, archetype, min_samples=None):
    """Kill gate: measured AND recently unprofitable (daemon.py).

    recent_pf covers the last RECENT_WINDOW closed trips; below
    `min_samples` the signal is noise → no kill (a fresh archetype must not
    ban itself out of the paper-sampling loop).
    """
    _, st = _bucket(reliability, archetype)
    samples = int(_num(st.get("samples")))
    if samples < (KILL_MIN_SAMPLES if min_samples is None else min_samples):
        return False
    recent_pf = st.get("recent_pf")
    return recent_pf is not None and _num(recent_pf) < 1.0


# ── persistence + archive (wt_reference semantics) ─────────────────────

def save(path, data):
    """Atomic JSON write; never raises."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data if isinstance(data, dict) else {}, fh,
                      indent=2, sort_keys=True)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def load(path):
    """Read the ledger; {} on any failure; never raises."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def archive_trades(path, archetype, trips):
    """Append closed trips of a rotated-out source under its archetype.

    Keyed through ledger_key(); bounded to ARCHIVE_MAX_PER_ARCHETYPE
    (oldest dropped); deduped by (strategy_id, close_ts). Never raises.
    """
    archetype = ledger_key(archetype)
    trips = [t for t in (trips or []) if isinstance(t, dict)]
    if not trips:
        return False
    try:
        archive = load_archive(path)
        rows = [r for r in archive.get(archetype, []) if isinstance(r, dict)]
        rows.extend(trips)
        seen, dedup = set(), []
        for t in rows:
            marker = (t.get("strategy_id"), t.get("close_ts"))
            if marker in seen and marker != (None, None):
                continue
            seen.add(marker)
            dedup.append(t)
        archive[archetype] = dedup[-ARCHIVE_MAX_PER_ARCHETYPE:]
        return save(path, archive)
    except Exception:
        return False


def load_archive(path):
    """Archived trips per canonical archetype; {} when absent."""
    data = load(path)
    return {k: [t for t in v if isinstance(t, dict)]
            for k, v in data.items() if isinstance(v, list)} if data else {}


def merge_archived(archive_by_archetype, sources):
    """Fold archived (rotated-out) trips into a sources list (append)."""
    for arch, trips in (archive_by_archetype or {}).items():
        if trips:
            sources.append({"archetype": arch, "trips": trips})
    return sources


# ── CLI ────────────────────────────────────────────────────────────────

def _parse_source(spec, loader, default_archetype, default_synthetic):
    m = _SOURCE_SPEC_RE.match(spec.strip())
    path = m.group("path")
    archetype = m.group("archetype") or default_archetype
    synthetic = default_synthetic
    # `!!real` / `!!synthetic` may close either the path or the label:
    # `zip=Label!!real` and `zip!!real` both parse.
    for suffix, flag in (("!!synthetic", True), ("!!real", False)):
        if archetype and archetype.endswith(suffix):
            archetype, synthetic = archetype[:-len(suffix)], flag
        if path.endswith(suffix):
            path, synthetic = path[:-len(suffix)], flag
    trips, report = loader(path, synthetic=synthetic)
    return {"archetype": archetype, "trips": trips,
            "report": report, "path": path}


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Reliability ledger — freqtrade grid round-trips "
                    "(M4)")
    ap.add_argument("--sqlite", action="append", default=[],
                    help="tradesv3.sqlite source; PATH or PATH=ARCHETYPE "
                    "(real samples; !!synthetic to override)")
    ap.add_argument("--backtest-zip", action="append", default=[],
                    help="backtesting export zip; PATH or PATH=ARCHETYPE "
                    "(synthetic seed by default; !!real for acceptance "
                    "math)")
    ap.add_argument("--archetype",
                    help="default archetype label for unlabeled sources")
    ap.add_argument("--archive", help="merge archived (rotated-out) "
                    "trips from this archive file")
    ap.add_argument("--save-archive",
                    help="append every ingested trip to this archive "
                    "(retire the source while keeping its evidence)")
    ap.add_argument("--out", help="ledger output path (merged over the "
                    "current file when it exists); default "
                    "grid/state/reliability.json")
    ap.add_argument("--report", action="store_true",
                    help="human summary per archetype (stats, ladder tier, "
                    "kill gate)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    sources = []
    for spec in args.sqlite:
        sources.append(_parse_source(spec, from_sqlite, args.archetype,
                                     default_synthetic=False))
    for spec in args.backtest_zip:
        sources.append(_parse_source(spec, from_backtest_zip, args.archetype,
                                     default_synthetic=True))
    if args.archive and Path(args.archive).exists():
        sources = merge_archived(load_archive(args.archive), sources)

    out_path = Path(args.out) if args.out else DEFAULT_LEDGER
    ledger = load(out_path) if out_path.exists() else {}
    if not sources:
        if not (ledger or args.report):
            ap.print_help()
            return 2
    else:
        # merge trips per canonical key FIRST: update() REPLACES a bucket
        # with the stats of the trips it is given (wt_reference
        # semantics), so two sources of the same archetype must be
        # combined before the single update
        by_key: dict = {}
        for src in sources:
            by_key.setdefault(ledger_key(src["archetype"]), []).extend(
                src["trips"])
        for key, trips in by_key.items():
            update(ledger, key, trips)

    if args.save_archive and sources:
        for src in sources:
            archive_trades(args.save_archive, src["archetype"], src["trips"])

    if sources or args.out:
        save(out_path, ledger)

    if not args.quiet:
        for src in sources:
            r = src["report"]
            print(f"[{r['source_kind']}] {src['path']}  archetype="
                  f"{ledger_key(src['archetype'])}")
            print(f"  closed trades={r['trades_closed']}  open skipped="
                  f"{r['trades_open_skipped']}  tp trips={r['tp_trips']}"
                  f"  full-exit trips={r['full_exit_trips']}"
                  f"  surprises={len(r['surprises'])}")
            for s in r["surprises"][:5]:
                print(f"    ! {s}")
    if args.report:
        for key in sorted(ledger):
            st = ledger[key]
            if not isinstance(st, dict) or "samples" not in st:
                continue
            mult, tier, _ = size_multiplier(ledger, key)
            killed = refuse_new_archetype(ledger, key)
            print(f"{key}: samples={st.get('samples')} (+"
                  f"{st.get('synthetic_samples')} synth)  PF="
                  f"{st.get('profit_factor')}  recent_pf={st.get('recent_pf')}"
                  f"  win={st.get('win_rate')}  E=$"
                  f"{st.get('expectancy_usd')}/trip  maxDD=$"
                  f"{st.get('max_dd_usd')}  tier={tier} "
                  f"({'KILL' if killed else 'ok'})")
    if sources or args.report:
        print(f"ledger: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
