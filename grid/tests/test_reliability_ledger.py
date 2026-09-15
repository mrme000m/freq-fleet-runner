#!/usr/bin/env python3
"""M4 unit tests — pairing engine + ledger + gates + archive + CLI.

Offline: the SQLite source is exercised against schema-subset temp DBs,
the backtest-zip source against a synthetic export built in tmp_path.
The real-export acceptance numbers (127/21 trips, PnL reconciliation)
live in test_m4_acceptance.py, skipped when the ft_user_data scratch is
absent.
"""
import json
import os
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

from grid.reliability import ledger as L
from grid.reliability import pairing as P


# ── sqlite fixture helpers (freqtrade trades+orders schema subset) ──────

def make_db(path, trades):
    """trades: [{id, is_open, is_short, open/close_date, exit_reason,
    open_rate, close_rate, fee_open, fee_close, orders: [(side, tag,
    px, qty, ts)]}]"""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE trades (id INTEGER PRIMARY KEY, pair TEXT,
            is_open BOOLEAN, is_short BOOLEAN, open_date TEXT,
            close_date TEXT, exit_reason TEXT, enter_tag TEXT,
            open_rate FLOAT, close_rate FLOAT, fee_open FLOAT,
            fee_close FLOAT, leverage FLOAT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, ft_trade_id INTEGER,
            ft_order_side TEXT, ft_order_tag TEXT, average FLOAT,
            price FLOAT, stop_price FLOAT, ft_price FLOAT, filled FLOAT,
            amount FLOAT, order_filled_date TEXT, order_date TEXT);
    """)
    for t in trades:
        conn.execute(
            "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.get("id", 1), "BTC/USDC:USDC", t.get("is_open", 0),
             t.get("is_short", 0), t.get("open_date", "2026-09-14 01:00:00+00:00"),
             t.get("close_date"), t.get("exit_reason"), "grid_recenter",
             t.get("open_rate", 100.0), t.get("close_rate"),
             t.get("fee_open", 0.001), t.get("fee_close", 0.001), 1.0))
        for i, (side, tag, px, qty, ts) in enumerate(t.get("orders", [])):
            conn.execute(
                "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (i + 1, t.get("id", 1), side, tag, px, None, None, None,
                 qty, qty, ts, ts))
    conn.commit()
    conn.close()


class TestSqlitePairing:
    def test_full_scenario(self, tmp_path):
        """anchor + L0 buy/sell + unsold L1 + channel-top full exit.

        L0: buy 0.2@99.0, TP sell 0.199@99.5 (stake-mechanics sliver:
        0.001 leftover merges into the same trip at the exit px 110),
        L1: buy 0.2@98.0 unsold, anchor 0.2@100.0.
        """
        db = str(tmp_path / "tradesv3.sqlite")
        make_db(db, [{
            "id": 7, "close_date": "2026-09-14 05:00:00+00:00",
            "exit_reason": "channel_top_exit",
            "orders": [
                ("buy", "grid_recenter", 100.0, 0.2, "2026-09-14 01:00:00+00:00"),
                ("buy", "grid_buy_L0", 99.0, 0.2, "2026-09-14 02:00:00+00:00"),
                ("sell", "grid_sell_L0", 99.5, 0.199, "2026-09-14 03:00:00+00:00"),
                ("buy", "grid_buy_L1", 98.0, 0.2, "2026-09-14 03:30:00+00:00"),
                ("sell", None, 110.0, 0.601, "2026-09-14 05:00:00+00:00"),
            ],
        }])
        trips, rep = P.from_sqlite(db)
        assert rep["trades_closed"] == 1 and rep["surprises"] == []
        assert rep["tp_trips"] == 1 and rep["full_exit_trips"] == 2
        tp = next(t for t in trips if t["kind"] == "tp")
        assert tp["line"] == 0
        # pnl: matched 0.199*(99.5-99) + sliver 0.001*(110-99)
        #      - fee_open*0.2*99 - fee_close*(0.199*99.5 + 0.001*110)
        expected = (0.199 * 0.5 + 0.001 * 11.0
                     - 0.001 * (0.2 * 99.0)
                     - 0.001 * (0.199 * 99.5 + 0.001 * 110.0))
        assert tp["pnl_usd"] == pytest.approx(expected, abs=1e-6)
        assert tp["gain_pct"] == pytest.approx((99.5 / 99.0 - 1) * 100)
        assert tp["strategy_id"] == "tradesv3#7-L0"
        fe = sorted((t for t in trips if t["kind"] == "full_exit"),
                    key=lambda t: (t["line"] is None, t["line"]))
        assert fe[0]["line"] == 1 and fe[1]["line"] is None  # anchor last
        assert fe[0]["exit_reason"] == "channel_top_exit"
        assert fe[0]["pnl_usd"] == pytest.approx(
            0.2 * (110.0 - 98.0) - 0.001 * (0.2 * 98.0) - 0.001 * (0.2 * 110.0),
            abs=1e-6)
        assert fe[1]["pnl_usd"] == pytest.approx(
            0.2 * (110.0 - 100.0) - 0.001 * 20.0 - 0.001 * 22.0, abs=1e-6)

    def test_open_trade_skipped(self, tmp_path):
        db = str(tmp_path / "t.sqlite")
        make_db(db, [{
            "id": 1, "is_open": 1, "close_date": None, "exit_reason": None,
            "orders": [("buy", "grid_recenter", 100.0, 0.2, "2026-09-14 01:00:00+00:00")],
        }])
        trips, rep = P.from_sqlite(db)
        assert trips == [] and rep["trades_open_skipped"] == 1

    def test_sell_without_buy_drains_pool_with_surprise(self, tmp_path):
        """A grid_sell on a never-bought line still sold REAL position qty:
        it drains the fungible pool (the anchor) exactly, and the state
        anomaly is surfaced as a surprise — no approx basis guessing."""
        db = str(tmp_path / "t.sqlite")
        make_db(db, [{
            "id": 3, "close_date": "2026-09-14 05:00:00+00:00",
            "exit_reason": "force_exit", "open_rate": 100.0,
            "orders": [
                ("buy", "grid_recenter", 100.0, 0.2, "2026-09-14 01:00:00+00:00"),
                ("sell", "grid_sell_L5", 101.0, 0.1, "2026-09-14 02:00:00+00:00"),
                ("sell", None, 99.0, 0.1, "2026-09-14 05:00:00+00:00"),
            ],
        }])
        trips, rep = P.from_sqlite(db)
        assert rep["surprises"] and "no grid_buy on the line" \
            in rep["surprises"][0]
        tp = next(t for t in trips if t["kind"] == "tp")
        assert tp["line"] == 5 and tp["qty"] == pytest.approx(0.1)
        assert tp["buy_px"] == 100.0  # drained from the anchor lot
        fe = next(t for t in trips if t["kind"] == "full_exit")
        assert fe["qty"] == pytest.approx(0.1)  # anchor remainder
        assert sum(t["pnl_usd"] for t in trips) == pytest.approx(
            0.1 * 1.0 + 0.1 * (99.0 - 100.0)
            - 0.001 * (0.2 * 100.0)
            - 0.001 * (0.1 * 101.0 + 0.1 * 99.0), abs=1e-6)

    def test_closed_trade_never_closed_lot_excluded(self, tmp_path):
        db = str(tmp_path / "t.sqlite")
        make_db(db, [{
            "id": 4, "close_date": "2026-09-14 05:00:00+00:00",
            "exit_reason": "stopped", "close_rate": None,
            "orders": [
                ("buy", "grid_recenter", 100.0, 0.2, "2026-09-14 01:00:00+00:00"),
                ("buy", "grid_buy_L2", 99.0, 0.2, "2026-09-14 02:00:00+00:00"),
            ],
        }])
        trips, rep = P.from_sqlite(db)
        assert trips == []
        assert len(rep["surprises"]) == 2  # anchor + L2 lot

    def test_sqlite_synthetic_flag(self, tmp_path):
        db = str(tmp_path / "t.sqlite")
        make_db(db, [{
            "id": 1, "close_date": "2026-09-14 05:00:00+00:00",
            "exit_reason": "force_exit",
            "orders": [
                ("buy", "grid_recenter", 100.0, 0.2, "2026-09-14 01:00:00+00:00"),
                ("sell", None, 101.0, 0.2, "2026-09-14 05:00:00+00:00"),
            ],
        }])
        trips, _ = P.from_sqlite(db, synthetic=True)
        assert all(t["synthetic"] for t in trips)
        trips, _ = P.from_sqlite(db)
        assert all(not t["synthetic"] for t in trips)


# ── backtest-zip export helpers (synthetic export built in tmp) ────────

def _bt_trade(tid, orders, exit_reason="channel_top_exit"):
    """One export-shaped trade: orders = [(side, tag, px, qty, epoch)]."""
    return {
        "trade_id": tid, "pair": "BTC/USDC:USDC", "is_open": False,
        "is_short": False, "exit_reason": exit_reason,
        "open_date": "2026-09-14 01:00:00+00:00",
        "open_timestamp": 1760000000,
        "close_date": "2026-09-14 05:00:00+00:00",
        "close_timestamp": 1760014400,
        "open_rate": 100.0, "close_rate": 101.0,
        "fee_open": 0.001, "fee_close": 0.001,
        "orders": [
            {"ft_order_side": side, "ft_order_tag": tag,
             "ft_is_entry": side == "buy", "safe_price": px,
             "amount": qty, "order_filled_timestamp": ts}
            for side, tag, px, qty, ts in orders
        ],
    }


def _zip_export(tmp_path, trades, name="backtest-result-x"):
    zp = tmp_path / f"{name}.zip"
    export = {"strategy": {"GridStrategy": {"trades": trades}}}
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("result.json", json.dumps(export))
    return str(zp)


class TestBacktestZipSource:
    def test_zip_pairing_and_synthetic_default(self, tmp_path):
        zp = _zip_export(tmp_path, [_bt_trade(1, [
            ("buy", "grid_recenter", 100.0, 0.2, 1760000000),
            ("buy", "grid_buy_L3", 99.0, 0.2, 1760000100),
            ("sell", "grid_sell_L3", 99.4, 0.2, 1760000200),
            ("sell", None, 101.0, 0.2, 1760014400),
        ])])
        trips, rep = P.from_backtest_zip(zp)
        assert rep["tp_trips"] == 1 and rep["full_exit_trips"] == 1
        assert rep["synthetic"] is True
        assert all(t["synthetic"] for t in trips)  # research seed default
        trips, _ = P.from_backtest_zip(zp, synthetic=False)
        assert all(not t["synthetic"] for t in trips)
        tp = next(t for t in trips if t["kind"] == "tp")
        assert tp["strategy_id"].startswith(Path(zp).stem)
        assert tp["gain_pct"] == pytest.approx((99.4 / 99.0 - 1) * 100,
                                               abs=1e-5)  # 6-dp rounded

    def test_zip_anchor_tag_is_not_a_grid_line(self, tmp_path):
        # anchor carries enter_tag 'grid_recenter' — must pair as anchor,
        # not crash the grid_(buy|sell)_L regex
        zp = _zip_export(tmp_path, [_bt_trade(2, [
            ("buy", "grid_recenter", 100.0, 0.2, 1760000000),
            ("sell", None, 102.0, 0.2, 1760014400),
        ])])
        trips, rep = P.from_backtest_zip(zp)
        assert rep["full_exit_trips"] == 1 and trips[0]["line"] is None


# ── ledger stats / keys / gates ────────────────────────────────────────

def _trip(pnl, close=0.0, kind="tp", gain=None, synthetic=False, sid="s"):
    return {"pnl_usd": pnl, "close_ts": close, "strategy_id": f"{sid}-{close}",
            "kind": kind, "gain_pct": gain, "synthetic": synthetic}


class TestStats:
    def test_pf_cap_and_win_rate(self):
        st = L.stats([_trip(1.0, 1), _trip(0.5, 2), _trip(0.25, 3)])
        assert st["samples"] == 3 and st["synthetic_samples"] == 0
        assert st["profit_factor"] == 99.0  # no losses → cap
        assert st["win_rate"] == 1.0
        assert st["expectancy_usd"] == pytest.approx(0.5833, abs=1e-3)
        assert st["gross_profit_usd"] == 1.75 and st["gross_loss_usd"] == 0.0

    def test_pf_with_losses_and_recent_window(self):
        trips = ([_trip(2.0, i) for i in range(10)]
                 + [_trip(-1.0, 100 + i) for i in range(11)])
        st = L.stats(trips)  # 21 trips; window keeps the last 20 → the
        # oldest trip (a win) drops out of recent_pf
        assert st["samples"] == 21
        assert st["profit_factor"] == round(20.0 / 11.0, 4)
        assert st["recent_pf"] == round(18.0 / 11.0, 4)
        assert st["win_rate"] == round(10 / 21, 4)
        assert st["max_dd_usd"] == pytest.approx(11.0)

    def test_stats_sorts_by_close_ts_itself(self):
        # shuffled input must give the same recent_pf as chronological
        trips = ([_trip(2.0, i) for i in range(10)]
                 + [_trip(-1.0, 100 + i) for i in range(11)])
        shuffled = list(reversed(trips))
        assert L.stats(shuffled)["recent_pf"] == L.stats(trips)["recent_pf"]

    def test_max_dd_curve(self):
        # cum: +1, +1.5, -0.5, -2 → peak 1.5, trough -2 → dd 3.5
        st = L.stats([_trip(1.0, 1), _trip(0.5, 2), _trip(-2.0, 3),
                     _trip(-1.5, 4)])
        assert st["max_dd_usd"] == pytest.approx(3.5)

    def test_synthetic_never_moves_metrics(self):
        st = L.stats([_trip(5.0, 1), _trip(-1.0, 2, synthetic=True),
                      _trip(-1.0, 3, synthetic=True)])
        assert st["samples"] == 1 and st["synthetic_samples"] == 2
        assert st["profit_factor"] == 99.0
        assert st["expectancy_usd"] == 5.0

    def test_grid_native_fields(self):
        st = L.stats([_trip(0.1, 1, kind="tp", gain=0.457),
                      _trip(0.2, 2, kind="tp", gain=0.3),
                      _trip(-0.05, 3, kind="full_exit")])
        assert st["tp_trips"] == 2 and st["full_exit_trips"] == 1
        assert st["avg_trip_gain_pct"] == pytest.approx(0.3785, abs=1e-3)
        assert "updated_at" in st

    def test_empty(self):
        st = L.stats([])
        assert st["samples"] == 0 and st["profit_factor"] == 0.0
        assert st["win_rate"] == 0.0 and "avg_trip_gain_pct" not in st


class TestLedgerKeyAndUpdate:
    def test_ledger_key_canonicalizes(self):
        assert L.ledger_key("trend_up") == "Long Grid / classic LONG"
        assert L.ledger_key("Long Grid / classic LONG") == \
            "Long Grid / classic LONG"
        assert L.ledger_key("some-new-regime") == "some-new-regime"
        assert L.ledger_key(None) == "unknown"

    def test_update_recomputes_and_no_erase(self):
        led = {}
        L.update(led, "trend_up", [_trip(1.0, 1)])
        assert "Long Grid / classic LONG" in led
        before = dict(led)
        L.update(led, "trend_up", [])  # quiet cycle → keep history
        assert led == before

    def test_update_replaces_bucket_cli_merges_same_key(self):
        # update() REPLACES a bucket's stats with the given trips
        # (wt_reference semantics) — merging several sources of the same
        # archetype is the CLI's job
        led = {}
        L.update(led, "trend_up", [_trip(1.0, 1)])
        L.update(led, "trend_up", [_trip(2.0, 2)])
        st = led["Long Grid / classic LONG"]
        assert st["samples"] == 1 and st["expectancy_usd"] == 2.0

    def test_regime_name_and_label_write_one_bucket(self):
        led = {}
        L.update(led, "trend_up", [_trip(1.0, 1)])
        assert list(led) == ["Long Grid / classic LONG"]  # canonicalized


class TestGates:
    def test_size_multiplier_tiers(self):
        led_probe = {"X": {"samples": 10, "profit_factor": 0.9}}
        led_full = {"X": {"samples": 30, "profit_factor": 1.31}}
        led_full_low_pf = {"X": {"samples": 40, "profit_factor": 1.29}}
        led_base = {"X": {"samples": 9, "profit_factor": 9.0}}
        assert L.size_multiplier(led_base, "X")[1] == "base"
        assert L.size_multiplier(led_probe, "X")[:2] == (0.40, "probe")
        assert L.size_multiplier(led_full, "X")[:2] == (0.50, "full")
        # PF below 1.3 keeps probe even with plenty of samples
        assert L.size_multiplier(led_full_low_pf, "X")[1] == "probe"
        assert L.size_multiplier(led_full, "X", {"full_pct": 0.6})[0] == 0.6

    def test_refuse_new_archetype(self):
        small = {"X": {"samples": 9, "recent_pf": 0.0}}
        losing = {"X": {"samples": 10, "recent_pf": 0.99}}
        healthy = {"X": {"samples": 30, "recent_pf": 1.2}}
        assert L.refuse_new_archetype(small, "X") is False   # noise floor
        assert L.refuse_new_archetype(losing, "X") is True   # KILL
        assert L.refuse_new_archetype(healthy, "X") is False
        assert L.refuse_new_archetype(losing, "X", min_samples=50) is False


class TestPersistenceAndArchive:
    def test_save_load_roundtrip(self, tmp_path):
        p = tmp_path / "rel.json"
        assert L.save(p, {"X": {"samples": 3}}) is True
        assert L.load(p)["X"]["samples"] == 3
        assert L.load(tmp_path / "absent.json") == {}

    def test_archive_dedup_and_bound(self, tmp_path):
        ap = tmp_path / "arch.json"
        trips = [_trip(1.0, i) for i in range(550)]
        assert L.archive_trades(ap, "trend_up", trips) is True
        arch = L.load_archive(ap)
        key = "Long Grid / classic LONG"
        assert len(arch[key]) == L.ARCHIVE_MAX_PER_ARCHETYPE  # bounded
        assert [t["pnl_usd"] for t in arch[key]][-1] == 1.0  # newest kept
        # re-archiving the same rows must not double-count
        L.archive_trades(ap, key, trips[-3:])
        assert len(L.load_archive(ap)[key]) == L.ARCHIVE_MAX_PER_ARCHETYPE

    def test_merge_archived_feeds_compute(self, tmp_path):
        ap = tmp_path / "arch.json"
        L.archive_trades(ap, "trend_up", [_trip(1.0, 1)])
        sources = L.merge_archived(L.load_archive(ap), [])
        led = L.compute(sources)
        assert led["Long Grid / classic LONG"]["samples"] == 1


class TestCli:
    def test_end_to_end_sources_and_out(self, tmp_path, monkeypatch):
        zp = _zip_export(tmp_path, [_bt_trade(1, [
            ("buy", "grid_recenter", 100.0, 0.2, 1760000000),
            ("buy", "grid_buy_L3", 99.0, 0.2, 1760000100),
            ("sell", "grid_sell_L3", 99.4, 0.2, 1760000200),
            ("sell", None, 101.0, 0.2, 1760014400),
        ])])
        out = tmp_path / "ledger.json"
        rc = L._cli([
            "--backtest-zip", f"{zp}=trend_up!!real", "--out", str(out)])
        assert rc == 0
        led = L.load(out)
        st = led["Long Grid / classic LONG"]
        assert st["samples"] == 2  # 1 tp + 1 full_exit
        # default (no !!real): backtest evidence is synthetic seed
        rc = L._cli(["--backtest-zip", f"{zp}=trend_up",
                     "--out", str(tmp_path / "l2.json")])
        led2 = L.load(tmp_path / "l2.json")
        st2 = led2["Long Grid / classic LONG"]
        assert st2["samples"] == 0 and st2["synthetic_samples"] == 2

    def test_two_sources_same_archetype_merge(self, tmp_path):
        za = _zip_export(tmp_path, [_bt_trade(1, [
            ("buy", "grid_recenter", 100.0, 0.2, 1760000000),
            ("sell", None, 101.0, 0.2, 1760014400)])], name="backtest-a")
        zb = _zip_export(tmp_path, [_bt_trade(1, [
            ("buy", "grid_recenter", 50.0, 0.4, 1760000000),
            ("sell", None, 51.0, 0.4, 1760014400)])], name="backtest-b")
        out = tmp_path / "l.json"
        rc = L._cli(["--backtest-zip", f"{za}=trend_up!!real",
                     "--backtest-zip", f"{zb}=trend_up!!real",
                     "--out", str(out)])
        assert rc == 0
        st = L.load(out)["Long Grid / classic LONG"]
        assert st["samples"] == 2  # merged, not overwritten
        assert st["expectancy_usd"] == pytest.approx(
            (0.2 * 1.0 + 0.4 * 1.0) / 2
            - 0.001 * 20.0 - 0.001 * 20.2, abs=1e-3)

    def test_merge_over_existing_keeps_other_buckets(self, tmp_path):
        out = tmp_path / "l.json"
        L.save(out, {"neutral": {"samples": 5}})
        zp = _zip_export(tmp_path, [_bt_trade(1, [
            ("buy", "grid_recenter", 100.0, 0.2, 1760000000),
            ("sell", None, 101.0, 0.2, 1760014400)])])
        L._cli(["--backtest-zip", f"{zp}=trend_up!!real",
                "--out", str(out)])
        led = L.load(out)
        assert led["neutral"]["samples"] == 5
        assert led["Long Grid / classic LONG"]["samples"] == 1
