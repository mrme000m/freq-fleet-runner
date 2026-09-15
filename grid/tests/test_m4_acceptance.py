#!/usr/bin/env python3
"""M4 acceptance — "ledger matches M1's backtest round-trip expectations"
(the milestone's acceptance clause in grid/docs/freqtrade-execution-
engine.md §M4).

Runs ONLY where the ft_user_data research scratch exists (the dev
machine); skips on fresh clones / CI. The expectations were verified with
the m1_reconcile.py oracle (exit 0, zero surprises) on the very zips
asserted here — this test pins the ledger against the same evidence:

tuned export (m1_grid.zip — the M2-tuned backtest, 2026-09-14):
  127 tp round-trips, every one a positive price gain, avg 0.457%/trip
  21 full-exit trips (11 unsold tagged lots + 10 anchors)
  zero surprises; sum(trip pnl) + funding == export profit_total_abs
  within amount-precision rounding

default-params export (m2_default_params.zip):
  71 tp round-trips (the M2 comparison row "defaults +8.23, 71 trips")

synthetic semantics: the same zip ingested WITHOUT !!real must produce
samples=0 / synthetic_samples=148 — backtest evidence never gates the
sizing ladder.
"""
import json
import os
import zipfile

import pytest

from grid.reliability import pairing as P
from grid.reliability import ledger as L

WS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUNED = os.path.join(WS, "ft_user_data", "backtest_results", "m1_grid.zip")
DEFAULTS = os.path.join(WS, "ft_user_data", "backtest_results",
                       "m2_default_params.zip")

pytestmark = pytest.mark.skipif(
    not os.path.exists(TUNED),
    reason="ft_user_data research scratch absent (fresh clone / CI)")


def _export(path):
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if (n.endswith(".json") and all(x not in n for x in
                    ("_config", "meta", "market_change", "_wallet"))):
                d = json.loads(z.read(n))
                if isinstance(d, dict) and "strategy" in d:
                    break
    key = next(k for k in d["strategy"] if "Grid" in k)
    return d["strategy"][key]


class TestTunedExport:
    def setup_method(self):
        self.trips, self.rep = P.from_backtest_zip(TUNED, synthetic=False)

    def test_trip_counts_match_oracle(self):
        assert self.rep["tp_trips"] == 127
        assert self.rep["full_exit_trips"] == 21
        assert self.rep["surprises"] == []
        assert self.rep["trades_open_skipped"] == 0

    def test_tp_gains_match_oracle(self):
        tp = [t for t in self.trips if t["kind"] == "tp"]
        gains = sorted(t["gain_pct"] for t in tp)
        assert all(g > 0 for g in gains)          # 127/127 won
        assert sum(gains) / len(gains) == pytest.approx(0.457, abs=0.001)

    def test_pnl_reconciles_with_export(self):
        st = _export(TUNED)
        funding = sum(t.get("funding_fees") or 0 for t in st["trades"])
        total = sum(t["pnl_usd"] for t in self.trips) + funding
        assert total == pytest.approx(st["profit_total_abs"], abs=0.05)

    def test_ledger_bucket_from_real_trips(self):
        led = L.compute([{"archetype": "trend_up", "trips": self.trips}])
        st = led["Long Grid / classic LONG"]
        assert st["samples"] == 148 and st["synthetic_samples"] == 0
        assert st["tp_trips"] == 127 and st["full_exit_trips"] == 21
        assert st["profit_factor"] > 1.3   # full tier on the ladder

    def test_synthetic_seed_never_gates(self):
        trips, rep = P.from_backtest_zip(TUNED)  # default synthetic=True
        led = L.compute([{"archetype": "trend_up", "trips": trips}])
        st = led["Long Grid / classic LONG"]
        assert st["samples"] == 0
        assert st["synthetic_samples"] == 148
        assert st["profit_factor"] == 0.0
        assert L.size_multiplier(led, "trend_up")[1] == "base"


class TestDefaultsExport:
    def test_71_tp_trips_match_m2_comparison_row(self):
        trips, rep = P.from_backtest_zip(DEFAULTS, synthetic=False)
        assert rep["tp_trips"] == 71
        assert rep["surprises"] == []
        st = _export(DEFAULTS)
        funding = sum(t.get("funding_fees") or 0 for t in st["trades"])
        total = sum(t["pnl_usd"] for t in trips) + funding
        assert total == pytest.approx(st["profit_total_abs"], abs=0.05)
