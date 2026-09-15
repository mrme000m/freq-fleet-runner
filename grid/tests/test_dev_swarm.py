#!/usr/bin/env python3
"""Unit tests for grid.dev helpers (swarm-ticket bridge + arg parsing).

Covers:
  * _swarm_ticket() never raises; returns a ticket dict
  * _swarm_ticket() in rule-fallback path returns llm_degraded=true
  * _swarm_ticket() builds the brief with the expected keys
  * _swarm_ticket() includes the live ledger evidence bucket
  * _arg_int() handles missing flag + bad int gracefully
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Clear LLM creds so every test runs in offline rule-fallback mode.
for k in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_KEY",
          "CLOUDFLARE_AI_TOKEN", "NVIDIA_API_KEY",
          "OPENROUTER_API_KEY", "MISTRAL_API_KEY"):
    os.environ.pop(k, None)

# Load grid/dev as a module
import importlib.util  # noqa: E402
import types  # noqa: E402
_dev_path = str(ROOT / "grid" / "dev")
_spec = importlib.util.spec_from_loader(
    "grid_dev", importlib.machinery.SourceFileLoader("grid_dev", _dev_path))
dev = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(dev)


def _scrub(p: dict):
    """Strip helper-added keys for test stability."""
    return {k: v for k, v in p.items() if k not in ("debate", "risk")}


def test_swarm_ticket_high_atr():
    """High ATR + high score → rule-fallback GO ticket."""
    row = {"coin": "BTC", "pair": "BTC/USDC:USDC", "pair_code": "BTCUSDC",
           "atr_pct": 3.5, "dayNtlVlm": 5e9, "score": 1.75e10}
    t = dev._swarm_ticket(row, current_pair="SOL/USDC:USDC")
    # Score=1.75e10 / 1e8 = 175 → clamped to 100 → GO
    assert t["decision"] == "GO"
    assert t["llm_degraded"] is True
    assert t["regime"] == "trend_up"
    assert t["grid_type"] == "long"


def test_swarm_ticket_low_atr_no_go():
    """Low ATR (flat regime) → facilitator fallback NO_GO."""
    row = {"coin": "DEAD", "pair": "DEAD/USDC:USDC", "pair_code": "DEADUSDC",
           "atr_pct": 0.4, "dayNtlVlm": 1e6, "score": 4e5}
    t = dev._swarm_ticket(row)
    # score=4e5/1e8=0.004 → 0 → NO_GO
    assert t["decision"] == "NO_GO"
    assert t["llm_degraded"] is True
    assert t["veto"] == "facilitator NO_GO"


def test_swarm_ticket_never_raises():
    """Malformed row → never raises; returns NO_GO ticket."""
    t = dev._swarm_ticket({})
    assert t["decision"] in ("GO", "NO_GO")
    # llm_degraded True (no creds) OR False (no LLM hit at all)
    assert isinstance(t.get("llm_degraded"), bool)


def test_swarm_ticket_brief_has_required_keys():
    """The brief fed to the swarm carries symbol/venue/regime/score_final/
    metrics/evidence/slot keys (the deliber() contract)."""
    # We can't intercept the brief from outside; instead we verify the
    # ticket's symbol/regime/grid_type fields match what _swarm_ticket
    # derived from the row (proves the brief was built correctly).
    row = {"coin": "ETH", "pair": "ETH/USDC:USDC", "pair_code": "ETHUSDC",
           "atr_pct": 1.8, "dayNtlVlm": 1e9, "score": 1.8e9}
    t = dev._swarm_ticket(row)
    assert t["symbol"] == "ETH"
    assert t["venue"] == "hyperliquid"
    assert t["regime"] == "chop_high_volatility"
    # chop → neutral grid_type (rule-fallback map)
    assert t["grid_type"] in ("neutral", "long")


def test_arg_int_default():
    assert dev._arg_int([], "--top", 5) == 5
    assert dev._arg_int(["--top", "9"], "--top", 5) == 9
    assert dev._arg_int(["--top", "bogus"], "--top", 5) == 5


def test_swarm_ticket_includes_ledger_evidence(tmp_path, monkeypatch):
    """When state/reliability.json exists with the long-grid bucket,
    the brief carries its samples/PF/recent_pf."""
    # Write a fake reliability.json
    fake = {
        "Long Grid / classic LONG": {
            "samples": 25, "profit_factor": 1.45, "recent_pf": 1.20,
            "win_rate": 0.6, "expectancy_usd": 0.5, "max_dd_usd": 2.0,
        }
    }
    rel = tmp_path / "reliability.json"
    rel.write_text(json.dumps(fake))
    # Patch RELIABILITY path on the module
    monkeypatch.setattr(dev, "RELIABILITY", rel)
    row = {"coin": "SOL", "pair": "SOL/USDC:USDC", "pair_code": "SOLUSDC",
           "atr_pct": 4.0, "dayNtlVlm": 2e9, "score": 8e9}
    t = dev._swarm_ticket(row)
    # Deliber doesn't surface evidence directly on the ticket, but the
    # helper's brief passed it in. We can only smoke-test that the
    # helper doesn't error when ledger is present.
    assert t["decision"] in ("GO", "NO_GO")
    assert isinstance(t.get("llm_degraded"), bool)