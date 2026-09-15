#!/usr/bin/env python3
"""Unit tests for grid.agents (LLM provider + swarm).

Covers:
  * LLM chain env-driven cred filter
  * chat() raises cleanly when no creds present
  * chat_json() retries on bad JSON, gives up after attempts
  * deliberate() degrades to rule-fallback when no chain available
  * deliberate() honors the spot-can't-short hard veto
  * deliberate() exposes the GO ticket shape (decision, grid_type,
    max_alloc_mult, step_mult, risk, debate, llm_degraded)
  * deliberate() rejects NO_GO tickets before calling risk team

Tests stub the provider chain (`_chain=[...]`) so they don't hit any
LLM endpoint and don't need keys. The rule-fallback tests run with
_chain=None and no creds in env → every stage falls back.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# ensure repo root on path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from grid.agents import chat, chat_json, deliberate  # noqa: E402
from grid.agents import llm as llm_mod  # noqa: E402
from grid.agents.llm import _providers, _has_creds  # noqa: E402


# ── provider chain ────────────────────────────────────────────────────


def test_no_creds_no_chain(monkeypatch):
    """With no creds in env, _providers() returns []."""
    for k in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_KEY",
              "CLOUDFLARE_AI_TOKEN", "NVIDIA_API_KEY",
              "OPENROUTER_API_KEY", "MISTRAL_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert _providers() == []
    assert _has_creds("mistral") is False


def test_mistral_creds_only(monkeypatch):
    """Only Mistral key present → chain has exactly one entry."""
    for k in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_KEY",
              "CLOUDFLARE_AI_TOKEN", "NVIDIA_API_KEY",
              "OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    chain = _providers()
    names = [n for n, _ in chain]
    assert names == ["mistral"]


def test_chat_raises_with_no_chain():
    """chat() raises RuntimeError when no providers are configured."""
    for k in list(os.environ):
        if k.startswith(("CLOUDFLARE_", "NVIDIA_", "OPENROUTER_", "MISTRAL_")):
            os.environ.pop(k, None)
    with pytest.raises(RuntimeError, match="no LLM providers"):
        chat("hello")


# ── chat_json retry behavior ──────────────────────────────────────────


def test_chat_json_retry_then_fail(monkeypatch):
    """chat_json retries `attempts` times when replies are unparseable."""
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")

    calls = {"n": 0}

    def fake_fn(messages, max_tokens):
        calls["n"] += 1
        return "not json at all"

    # Patch _providers to return one Mistral entry that always returns prose
    monkeypatch.setattr(llm_mod, "_providers", lambda: [("mistral", fake_fn)])
    with pytest.raises(RuntimeError, match="attempt"):
        chat_json([{"role": "user", "content": "x"}], attempts=2)
    assert calls["n"] == 2


# ── swarm (offline rule-fallback) ────────────────────────────────────


def _ensure_no_creds(monkeypatch):
    for k in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_KEY",
              "CLOUDFLARE_AI_TOKEN", "NVIDIA_API_KEY",
              "OPENROUTER_API_KEY", "MISTRAL_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def _stub_chain_with_json():
    """Returns a chain that always replies with strict JSON, so we test
    the pipeline plumbing (not the LLM) end-to-end."""
    def fake_fn(messages, max_tokens):
        # Reply with a minimal valid ticket-shape object so the swarm
        # can extract provider + parsed dict. The content is tailored
        # to be plausible enough that the pipeline accepts it.
        return json.dumps({"decision": "GO", "grid_type": "long",
                           "rationale": "ok", "confidence": 0.7})
    return [("stub", fake_fn)]


def test_deliberate_rule_fallback_go(monkeypatch):
    """When no creds + high score → facilitator fallback issues GO."""
    _ensure_no_creds(monkeypatch)
    brief = {"symbol": "BTC", "venue": "hyperliquid", "regime": "trend_up",
             "score_final": 100.0}
    t = deliberate(brief)
    assert t["decision"] == "GO"
    assert t["llm_degraded"] is True
    assert t["grid_type"] == "long"  # regime-trend-up → long
    # fallback risk team has approve=True, conservative stays at 0.5
    assert "risk" in t
    assert t["max_alloc_mult"] == 0.5  # tightest = conservative 0.5
    assert 0.0 < t["step_mult"] <= 2.0
    assert t["facilitator_llm"] == "rule-fallback"


def test_deliberate_rule_fallback_no_go(monkeypatch):
    """Score below gate → rule fallback returns NO_GO before risk team."""
    _ensure_no_creds(monkeypatch)
    brief = {"symbol": "DEAD", "venue": "hyperliquid",
             "regime": "neutral", "score_final": 10.0}
    t = deliberate(brief)
    assert t["decision"] == "NO_GO"
    assert t["veto"] == "facilitator NO_GO"
    assert "risk" not in t  # risk team not invoked on NO_GO
    assert t["llm_degraded"] is True


def test_deliberate_spot_short_veto(monkeypatch):
    """Conservative + binance spot + short → hard veto spot cannot short."""
    _ensure_no_creds(monkeypatch)
    brief = {"symbol": "FOO", "venue": "binance",
             "regime": "trend_down", "score_final": 100.0}
    t = deliberate(brief)
    # score is high → facilitator fallback says GO for trend_down
    # risk_review hard veto: spot cannot short
    assert t["decision"] == "NO_GO"
    assert "spot cannot short" in t["veto"]


def test_deliberate_stub_chain(monkeypatch):
    """Stubbed chain returns a real LLM-shaped reply; pipeline runs clean."""
    _ensure_no_creds(monkeypatch)
    chain = _stub_chain_with_json()
    brief = {"symbol": "X", "venue": "hyperliquid",
             "regime": "chop_high_volatility", "score_final": 50.0}
    t = deliberate(brief, _chain=chain)
    # Every stage hit the stub → llm_degraded stays False
    assert t["llm_degraded"] is False
    assert t["facilitator_llm"] == "stub"
    # Ticket shape complete
    for k in ("symbol", "venue", "regime", "decision", "grid_type",
              "rationale", "confidence", "debate", "risk",
              "max_alloc_mult", "step_mult"):
        assert k in t


def test_deliberate_debate_block_captures_openings(monkeypatch):
    """debate['bull_open'] / ['bear_open'] capture the openings even
    after rebuttal overwrites bull/bear."""
    _ensure_no_creds(monkeypatch)
    chain = _stub_chain_with_json()
    brief = {"symbol": "X", "venue": "hyperliquid",
             "regime": "neutral", "score_final": 50.0}
    t = deliberate(brief, _chain=chain)
    # bull_open / bear_open are dicts (from the stub or fallback)
    assert isinstance(t["debate"]["bull_open"], dict)
    assert isinstance(t["debate"]["bear_open"], dict)