#!/usr/bin/env python3
"""M4 reliability — daemon-callable seam for the FT fleet.

The grid-autonomy daemon's `reliability_cycle` calls `bot_trades` to feed
the sizing ladder + kill gate. The WT-era `bot_trades` shells out to
`wt_browser.py`; under GRID_EXECUTION_BACKEND=freqtrade, that subprocess
is unavailable. This module exposes a drop-in replacement that reads the
FT fleet's per-instance `tradesv3.sqlite` directly and pairs grid
round-trips using the same oracle (`grid.reliability.pairing.from_sqlite`).

Seam contract (matches `parse_trades` output that the daemon expects):
  bot_trades_ft(bot_code) -> [trade dict]
    where each trade = {pnl_usd, close_ts, entered_at, strategy_id,
                        synthetic?}

The daemon's `bot_trades` should be patched to:
    if os.environ.get("GRID_EXECUTION_BACKEND") == "freqtrade":
        from grid.reliability.ft_source import bot_trades_ft
        return bot_trades_ft(bot_code)
    return _run_wt(...)

This module is the seam; patching the daemon is the M3 agent's job
(grid-autonomy is owned by other agents per AGENTS.md).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parent.parent) not in sys.path:  # so `import grid.reliability.*` works
    sys.path.insert(0, str(HERE.parent.parent))

from grid.reliability.pairing import from_sqlite  # noqa: E402

# Default fleet root — the standalone workspace. Override via env so the
# M3 daemon's `config.yaml:freqtrade.fleet_root` wins when set.
DEFAULT_FLEET_ROOT = Path(
    "/Volumes/ExMac/code/grid/0/freqtrade/grid/state/ft_fleet")

# Canonical archetype label for the standalone long-grid fleet (mirrors
# grid/reliability/ledger.py:ARCHETYPE_LABELS so daemon-side
# `archetype_stats` bucketing lines up with `state/reliability.json`).
DEFAULT_ARCHETYPE = "Long Grid / classic LONG"


def _fleet_root() -> Path:
    raw = os.environ.get("GRID_FLEET_ROOT")
    return Path(raw) if raw else DEFAULT_FLEET_ROOT


def _sqlite_path(bot_code: str) -> Path | None:
    """Locate the per-instance tradesv3.sqlite under fleet_root/<bot_code>/."""
    root = _fleet_root()
    for cand in (root / bot_code / "tradesv3.sqlite",
                 root / bot_code / "tradesv3.dryrun.sqlite"):
        if cand.is_file():
            return cand
    return None


def _orders_rows(db_path: Path) -> list[dict]:
    """Raw order rows from freqtrade's SQLite (mirrors
    freqtrade_backend.sqlite_orders enough for `from_sqlite`)."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = con.execute(
            "SELECT id, trade_id, ft_order_tag, ft_is_entry, status, "
            "average, cost, order_date FROM orders "
            "WHERE status='closed' ORDER BY order_date")
        cols = ("id", "trade_id", "ft_order_tag", "ft_is_entry", "status",
                "average", "cost", "order_date")
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        con.close()


def _trades_rows(db_path: Path) -> list[dict]:
    """Closed trade rows from freqtrade's SQLite (for fee_open/fee_close,
    is_open, close_timestamp — `from_sqlite` consumes these)."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = con.execute(
            "SELECT id, pair, is_open, amount, stake_amount, "
            "open_rate, close_rate, fee_open, fee_close, "
            "open_date, close_date, sell_reason "
            "FROM trades WHERE close_date IS NOT NULL "
            "ORDER BY close_date")
        cols = ("id", "pair", "is_open", "amount", "stake_amount",
                "open_rate", "close_rate", "fee_open", "fee_close",
                "open_date", "close_date", "sell_reason")
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        con.close()


def bot_trades_ft(bot_code: str,
                  archetype: str | None = None,
                  db_path: Path | None = None) -> list[dict]:
    """Closed round-trips for one FT bot — daemon seam.

    Returns [{pnl_usd, close_ts, entered_at, strategy_id, synthetic?}]
    in the exact shape `reliability_grid.parse_trades` emits, so the
    daemon's `_stats` / `archetype_stats` / sizing ladder need no change.

    Reads tradesv3.sqlite (or tradesv3.dryrun.sqlite) directly under
    GRID_FLEET_ROOT/<bot_code>/ — never raises, returns [] on miss.
    """
    arch = archetype or DEFAULT_ARCHETYPE
    path = db_path or _sqlite_path(bot_code)
    if path is None:
        return []
    try:
        orders = _orders_rows(path)
        trades = _trades_rows(path)
        # from_sqlite returns (trips, report); each trip already has
        # pnl_usd, close_ts, entered_at, strategy_id, synthetic in the
        # WT-compatible shape (pairing.py:45-57).
        trips, _report = from_sqlite(str(path), synthetic=False,
                                     strategy_id=bot_code)
        # Stamp strategy_id + bot_code for the daemon's bucketing.
        for t in trips:
            t["strategy_id"] = bot_code
            t["archetype"] = arch
        return trips
    except Exception:
        return []


def fleet_bot_codes(fleet_root: Path | None = None) -> list[str]:
    """Enumerate every bot_code under the fleet root (for the daemon's
    reliability_cycle to iterate). Excludes `archive/` (rotated-out
    instances whose trips are already in the archive file)."""
    root = fleet_root or _fleet_root()
    if not root.is_dir():
        return []
    out = []
    for entry in root.iterdir():
        if entry.name == "archive" or not entry.is_dir():
            continue
        if _sqlite_path(entry.name) is not None:
            out.append(entry.name)
    return sorted(out)


if __name__ == "__main__":
    # Smoke: list bot codes + round-trip count per code.
    for code in fleet_bot_codes():
        trips = bot_trades_ft(code)
        real = [t for t in trips if not t.get("synthetic")]
        pnl = sum(t.get("pnl_usd", 0.0) for t in real)
        print(f"{code}: {len(real)} real trip(s)  pnl_usd={round(pnl, 4)}")