#!/usr/bin/env python3
"""Offline tests for the console: the retired ctl plane (/api/status and
/api/observe fail-soft, /api/ctl/* semantics), the standalone PnL timeline
(tolerant _pnl_points shaping + the fleet-cumulative trades-DB series),
recommendation apply-gate verdicts, the live-first reliability payload
(file ledger enrichment), and the optimizer's not-applicable payload.

Run:  python3 -m unittest discover -s console -p "test_upgrade.py"
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))   # repo root → grid pkg
sys.path.insert(0, os.path.dirname(HERE))          # grid root → config_lite
sys.path.insert(0, HERE)                            # yaml_edit

import console.server as server                     # noqa: E402

CONFIG_TMPL = """\
portfolio:
  total_usd: 600.0
  slots_default: 4
optimizer:
  enabled: true
position_optimizer:
  enabled: true
  apply: false
  max_apply_per_day: 4
"""


class UpgradeTestCase(unittest.TestCase):
    """Isolated server globals: temp state dir, dead ctl/PB ports."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="console-upgrade-")
        self._saved = {
            "STATE_DIR": server.STATE_DIR, "CONFIG_PATH": server.CONFIG_PATH,
            "PB_URL": server.PB_URL, "PB_ENV_PATH": server.PB_ENV_PATH,
            "KILL_FILE": server.KILL_FILE,
            "_REL_CACHE": server._REL_CACHE, "_GEOM_CACHE": server._GEOM_CACHE,
        }
        server.STATE_DIR = os.path.join(self.tmp, "state")
        server.CONFIG_PATH = os.path.join(self.tmp, "config.yaml")
        server.PB_URL = "http://127.0.0.1:59999"          # dead port
        server.PB_ENV_PATH = os.path.join(self.tmp, "pb.env")
        server.KILL_FILE = os.path.join(self.tmp, "KILL")  # never the real grid/KILL
        server._REL_CACHE = None
        server._GEOM_CACHE = {}
        os.makedirs(server.STATE_DIR, exist_ok=True)
        with open(server.CONFIG_PATH, "w") as f:
            f.write(CONFIG_TMPL)
        # inert while CTL_RETIRED is True (no ctl call is ever placed), but
        # keeps the tests isolated if the plane is ever re-enabled locally
        server._ctl_port = lambda: 59999
        server._launchd_managed = lambda: False
        server._pid = lambda: None
        server._CTL_CACHE.clear()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(server, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_state(self, state):
        with open(os.path.join(server.STATE_DIR, "state.json"), "w") as f:
            json.dump(state, f)

    # ── shaping ──────────────────────────────────────────────────────

    def _mk_ft_db(self, d, closes):
        """Minimal dry-run trades DB with the columns pnl_payload reads."""
        os.makedirs(d, exist_ok=True)
        con = sqlite3.connect(os.path.join(d, "tradesv3.dryrun.sqlite"))
        con.executescript(
            "CREATE TABLE trades (is_open INTEGER, close_date TEXT,"
            " close_profit_abs REAL, stake_amount REAL, open_date TEXT,"
            " enter_tag TEXT, pair TEXT, exit_reason TEXT);"
            "CREATE TABLE orders (order_filled_date TEXT, ft_order_tag TEXT,"
            " ft_order_side TEXT, ft_pair TEXT, cost REAL, average REAL);")
        for at, profit in closes:
            con.execute("INSERT INTO trades (is_open, close_date,"
                        " close_profit_abs) VALUES (0, ?, ?)", (at, profit))
        con.commit()
        con.close()

    def test_pnl_points_tolerant_shape(self):
        # _pnl_points normalizes PB journal rows: payload at the top level,
        # inside `extra` (the PB free field), or absent entirely; newest first
        pts = server._pnl_points([
            {"kind": "pnl-snapshot", "at": "2026-09-06T01:00:00+00:00",
             "fleet": {"net": 1.5, "realized": 1.0, "unrealized": 0.5,
                       "committed_usd": 250.0, "idle_usd": 350.0}},
            {"kind": "pnl-snapshot", "at": "2026-09-06T01:05:00+00:00",
             "extra": {"fleet": {"net": 2.0}}},
            {"kind": "pnl-snapshot", "at": "2026-09-06T01:10:00+00:00"},
        ])
        self.assertEqual([p["at"] for p in pts],
                         ["2026-09-06T01:10:00+00:00",
                          "2026-09-06T01:05:00+00:00",
                          "2026-09-06T01:00:00+00:00"])   # newest first
        self.assertIsNone(pts[0]["fleet"])
        self.assertEqual(pts[1]["fleet"]["net"], 2.0)
        self.assertEqual(pts[2]["fleet"]["idle_usd"], 350.0)

    def test_pnl_timeline_is_fleet_cumulative(self):
        # ONE merged fleet series: a close on any instance advances the
        # fleet-wide cumulative realized. The old per-bot series made the
        # frontend chart sawtooth between each bot's own cumulative sum.
        base = os.path.join(self.tmp, "ft")
        self._mk_ft_db(os.path.join(base, "a"),
                       [("2026-09-15 01:00:00", 1.0),
                        ("2026-09-15 02:00:00", 1.0)])
        self._mk_ft_db(os.path.join(base, "b"),
                       [("2026-09-15 01:30:00", 0.5)])
        os.makedirs(os.path.join(server.STATE_DIR, "ft_fleet"), exist_ok=True)
        with open(os.path.join(server.STATE_DIR, "ft_fleet",
                               "registry.json"), "w") as f:
            json.dump({"instances": {
                "ft-a": {"bot_code": "ft-a",
                         "dir": os.path.join(base, "a"), "port": 18191},
                "ft-b": {"bot_code": "ft-b",
                         "dir": os.path.join(base, "b"), "port": 18192},
            }}, f)
        out = server.pnl_payload()
        self.assertEqual(out["source"], "freqtrade-dryrun")
        pts = out["points"]                       # newest first
        self.assertEqual([p["fleet"]["realized"] for p in reversed(pts)],
                         [1.0, 1.5, 2.5])         # 01:00, 01:30, 02:00
        for p in pts:
            self.assertEqual(p["fleet"]["net"], p["fleet"]["realized"])
        self.assertNotIn("live", pts[0])          # no open trades → no live pt

    def test_pnl_payload_empty_is_valid(self):
        # no PB rows (dead port), no fleet DBs → honest empty timeline
        out = server.pnl_payload()
        self.assertEqual(out["points"], [])
        self.assertEqual(out["source"], "freqtrade-dryrun")
        self.assertEqual(out["total"], 0)

    def test_recommendations_blocked_by_verdicts(self):
        # `at` stamps must be TODAY (utc): persisted_today counts records
        # whose `at` date prefix matches the current UTC date
        today = server.utcnow()[:10]
        recs = [
            {"at": f"{today}T01:00:00+00:00", "applied": True,
             "applied_at": f"{today}T01:01:00+00:00"},
            {"at": f"{today}T02:00:00+00:00"},   # no applied key at all
            {"at": f"{today}T03:00:00+00:00"},
        ]
        # recommendations_payload reads through the pbclient/_pb_get ladder
        # (not _http_json — that mock went stale when the PB auth ladder
        # landed); _pb_client returns None against the dead test PB_URL,
        # so the raw-HTTP _pb_get fallback is the path this exercises.
        real_pb_get = server._pb_get
        server._pb_get = (lambda url, timeout=2.5, token=None:
                           (True, {"items": [dict(r) for r in recs]}))
        try:
            payload = server.recommendations_payload(100)
        finally:
            server._pb_get = real_pb_get
        self.assertEqual(payload["apply"], False)          # advisory template
        self.assertEqual(payload["persisted_today"], 3)
        self.assertEqual(payload["recommendations"][0]["blocked_by"], "applied")
        self.assertEqual(payload["recommendations"][0]["applied_at"],
                         f"{today}T01:01:00+00:00")
        for r in payload["recommendations"][1:]:
            self.assertEqual(r["blocked_by"], "apply disabled")
            self.assertFalse(r["applied"])

    def test_reliability_live_first_file_ledger_enriched(self):
        # the primary `archetypes` table is the LIVE M4 ledger (no fleet
        # DBs in this env → empty, never a 500); the WT-era file snapshot
        # renders as `file_ledger` with the real/synthetic split + tiers
        with open(os.path.join(server.STATE_DIR, "reliability.json"), "w") as f:
            json.dump({"chop": {"samples": 11, "synthetic_samples": 8,
                                "profit_factor": 1.4},
                       # recent_pf present → not kill-flagged; 3 samples → base
                       "clean": {"samples": 3, "recent_pf": 1.05}}, f)
        out = server.reliability_payload()
        self.assertEqual(out["archetypes"], {})
        self.assertEqual(out["file_ledger"]["chop"]["real_samples"], 3)
        self.assertEqual(out["file_ledger"]["chop"]["tier"], "killed")
        self.assertEqual(out["file_ledger"]["clean"]["real_samples"], 3)
        self.assertEqual(out["file_ledger"]["clean"]["tier"], "base")
        self.assertIsNotNone(out["file_freshness"])
        self.assertIn("ladder", out)
        self.assertIn("kill_thresholds", out)

    # ── HTTP (fail-soft against dead ctl/PB) ─────────────────────────

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, path):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")

    def test_status_and_observe_fail_soft_200(self):
        for path in ("/api/status", "/api/observe"):
            code, body = self.call(path)
            self.assertEqual(code, 200)          # never a 500/502
            self.assertIn("error", body)

    def test_pnl_endpoint_valid_json_empty(self):
        code, body = self.call("/api/pnl")
        self.assertEqual(code, 200)
        self.assertEqual(body["points"], [])

    def test_optimizer_not_applicable_payload(self):
        # standalone contract: the WT-era slow-loop optimizer has no
        # freqtrade counterpart — 200 + {optimizer: null, applicable: false,
        # fast{geom, tuned_params, reliability, decisions_tail}}, never a 502
        code, body = self.call("/api/optimizer")
        self.assertEqual(code, 200)
        self.assertIsNone(body.get("optimizer"))
        self.assertFalse(body.get("applicable"))
        self.assertIn("reason", body)
        fast = body.get("fast") or {}
        self.assertEqual(fast.get("geom"), [])
        self.assertEqual(fast.get("tuned_params"), {})
        self.assertEqual(fast.get("decisions_tail"), [])

    def test_position_sweeps_fail_soft_200(self):
        """The sweep-history endpoint reads state.json directly (no ctl
        round-trip) — it must answer 200 even with the daemon down."""
        self.write_state({"journal": [
            {"kind": "position-optimizer-sweep", "msg": "2 bots analyzed",
             "at": "2026-09-07T01:00:00+00:00"},
            {"kind": "heartbeat", "msg": "score 88/100",
             "at": "2026-09-07T01:05:00+00:00"},   # not a sweep: excluded
        ]})
        code, body = self.call("/api/position-sweeps")
        self.assertEqual(code, 200)
        self.assertEqual(len(body["sweeps"]), 1)
        self.assertEqual(body["sweeps"][0]["kind"],
                         "position-optimizer-sweep")

    def test_status_proxy_keys_added_only_when_daemon_up(self):
        """ctl is a dead port here → /api/status stays the documented
        fail-soft {"error": ...} + 200 shape (backward compat: no 502)."""
        code, body = self.call("/api/status")
        self.assertEqual(code, 200)
        self.assertIn("error", body)

    def test_ctl_optimize_502_when_ctl_retired(self):
        # POST /api/ctl/optimize mirrors /api/ctl/rescreen: the ctl plane is
        # retired in this workspace → honest 502, never a cross-system POST
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/ctl/optimize",
            data=b"{}", method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                code, body = resp.status, json.loads(resp.read() or b"{}")
            self.fail("expected HTTPError 502")
        except urllib.error.HTTPError as exc:
            code = exc.code
            body = json.loads(exc.read() or b"{}")
        self.assertEqual(code, 502)
        self.assertTrue(str(body.get("error", "")).startswith("ctl retired"))
        self.assertIn("detail", body)

    def test_ctl_kill_is_local_file_only(self):
        # /api/ctl/kill must write the LOCAL KILL file — never POST to
        # :8799, which belongs to the M3 companion repo's daemon — and
        # /api/ctl/unkill must remove it.
        def post(path, payload):
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}{path}",
                data=json.dumps(payload).encode(), method="POST",
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read() or b"{}")
        code, body = post("/api/ctl/kill", {"confirm": True})
        self.assertEqual(code, 200)
        self.assertEqual(body.get("via"), "direct")
        self.assertTrue(os.path.exists(server.KILL_FILE))
        code, body = post("/api/ctl/unkill", {"confirm": True})
        self.assertEqual(code, 200)
        self.assertFalse(os.path.exists(server.KILL_FILE))


if __name__ == "__main__":
    unittest.main()
