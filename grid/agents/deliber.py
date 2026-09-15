#!/usr/bin/env python3
"""grid.agents.deliber — TradingAgents-pattern swarm for one rotation candidate.

Ported from grid-autonomy/agents/swarm.py (unchanged behavior; this repo
hosts the FT-fleet side of the same autonomy loop).

Pipeline per candidate (max 8 LLM calls, then deterministic fallback):
  Bull open → Bear open → Bull rebuttal → Bear rebuttal → Facilitator verdict
  → Risk team (seeking / neutral / conservative) → trade_ticket or veto.

Every agent returns STRICT JSON (schemas below). Any LLM failure degrades
to the rule-based fallback (regime→grid map from the playbook), so the
rotator never blocks on an LLM outage — it just logs llm_degraded: true.

Usage:
  from grid.agents import deliberate
  ticket = deliberate(brief)                  # uses live provider chain
  ticket = deliberate(brief, _chain=[...])    # stubbed in tests
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:  # so `grid.agents.llm` resolves
    sys.path.insert(0, str(HERE.parent))

from grid.agents.llm import chat_json  # noqa: E402

# Lower-TF analysis base: every swarm decision grounds itself in the
# Multi-TF pack (1m + 5m + 15m) so the LLM sees structure + execution,
# not just a single bar. The pack is built once per candidate and
# threaded through bull/bear/facilitator/risk prompts via
# brief["market_context"] — single source of truth, no per-agent drift.
try:
    from grid.screen import multi_tf_pack  # noqa: E402
except Exception:  # pragma: no cover — fallback path for unit tests
    multi_tf_pack = None

GRID_TYPE = {
    "chop_high_volatility": "neutral",
    "squeeze": "neutral",
    "neutral": "neutral",
    "trend_up": "long",
    "trend_down": "short",
}

SYS = ("You are a crypto grid-trading analyst. Reply with STRICT JSON only, "
       "no markdown fences, no commentary. Obey the requested schema exactly. "
       "Be terse: every string value at most 25 words, arrays at most 3 items.")


def _call(messages, schema_hint, _chain, fallback, role=None):
    """Call chat_json once; degrade to `fallback` on any failure.

    Returns (parsed, degraded_bool). The schema_hint arg is reserved for
    future per-agent schema validation; chat_json enforces strict JSON
    today, so the hint is currently unused (kept for the API parity with
    the daemon-side port).
    """
    try:
        _name, obj = chat_json(messages, _chain=_chain, role=role)
        if not isinstance(obj, dict):
            raise ValueError("non-dict reply")
        obj["_llm"] = _name
        return obj, False
    except Exception:
        fb = dict(fallback)
        fb["_llm"] = "rule-fallback"
        return fb, True


def _try_reflect():
    """Lazy import of reflect.memories_for; None when unavailable.

    The reflect module is not ported in this initial drop — it lives in
    grid-autonomy and is coupled to daemon state. Swarm callers see
    `memory` as None; the brief-text path still works, just without the
    past-outcomes sentence. Once reflect is ported (next iteration),
    this resolver picks it up automatically.
    """
    try:
        from grid.agents.deliber import reflect as _r
        return getattr(_r, "memories_for", None)
    except Exception:
        return None


def _memory_list(brief):
    fn = _try_reflect()
    if fn is None:
        return None
    try:
        mem = fn(brief)
        return mem or None
    except Exception:
        return None


def _memory_sentence(brief):
    mem = _memory_list(brief)
    if not mem:
        return ""
    return " Past outcomes for this token/venue (learn): " + json.dumps(mem) + "."


def brief_text(brief):
    m = brief.get("metrics", {})
    obj = {
        "symbol": brief.get("symbol"), "venue": brief.get("venue"),
        "regime": brief.get("regime"), "score": brief.get("score_final"),
        "price": m.get("price"), "atr_pct": m.get("atr_pct"),
        "adx": m.get("adx14"), "rsi": m.get("rsi14"),
        "bb_pctile": m.get("bb_width_pctile"),
        "spread_pct": brief.get("spread_pct"),
        "grid_step_pct": brief.get("step"),
        "flags": brief.get("flags"),
        "expected_fills_per_24h": brief.get("expected_fills_per_24h"),
        "harvest_net_pct_24h": brief.get("harvest_net_pct_24h"),
        "confluence": brief.get("confluence_notes"),
        "evidence": brief.get("evidence"),
        "stagnation": brief.get("stagnation_policy"),
        "slot": brief.get("slot"),
    }
    memory = _memory_list(brief)
    if memory:
        obj["memory"] = memory
    market_context = brief.get("market_context")
    if isinstance(market_context, dict) and market_context:
        obj["market_context"] = market_context
    return json.dumps(obj)


# --- Multi-TF analysis pack ---------------------------------------------

def _summarize_pack(pack):
    """Compress a Multi-TF pack into a prompt-friendly summary.

    Drops the raw OHLC points (LLMs reason better over trend slopes
    + regime tags than over 60 number tuples) and computes a per-TF
    trend slope so every agent sees the same lower-TF context."""
    if not pack or not isinstance(pack, dict):
        return None
    tfs = pack.get("tfs") or {}
    out = {"coin": pack.get("coin"), "atr_pct_aggregate": pack.get("atr_pct"),
           "tfs": {}}
    for tf, blob in tfs.items():
        pts = blob.get("points") or []
        if len(pts) >= 2:
            first, last = pts[0][4], pts[-1][4]
            slope = ((last - first) / first * 100.0) if first else 0.0
        else:
            slope = 0.0
        out["tfs"][tf] = {
            "close": blob.get("close"),
            "atr_pct": blob.get("atr_pct"),
            "trend_pct": round(slope, 3),
            "n_bars": len(pts),
        }
    return out


def attach_multi_tf(brief, coin=None, tfs=None):
    """Attach a Multi-TF analysis pack to `brief` in-place.

    The pack is the SINGLE source of candle truth for every swarm
    agent — the 1m + 5m + 15m bar sets the LLM's view of structure,
    regime, and execution micro-structure. Without it the swarm was
    blind to the lower-TF band the engines now trade on.

    Returns the (possibly mutated) brief. If the pack can't be fetched
    (HL outage, offline), `brief` is returned unchanged — the swarm
    then runs on the legacy metrics-only path, same as before the
    lower-TF reset."""
    if multi_tf_pack is None:
        return brief
    target = coin or brief.get("symbol") or brief.get("pair_code")
    if not target:
        return brief
    try:
        pack = multi_tf_pack(target, tfs=tfs)
        brief["market_context"] = _summarize_pack(pack)
        brief["market_context_full"] = pack  # full pack for the rotator
    except Exception:
        # Best-effort: swarm still has metrics; degrade gracefully.
        pass
    return brief


# --- per-agent deliberation ---------------------------------------------

def bull_open(brief, _chain=None):
    fb = {"side": "long" if brief.get("regime") != "trend_down" else "short",
          "thesis": "rule-fallback: regime has harvestable range",
          "invalidation": "regime switch or spread > step",
          "confidence": 0.5}
    return _call([
        {"role": "system", "content": SYS},
        {"role": "user", "content":
         f"Argue FOR deploying a grid bot on this candidate. Schema: "
         f'{{"side":"long|short|neutral","thesis":str,"invalidation":str,'
         f'"confidence":0-1}}. Candidate: {brief_text(brief)}{_memory_sentence(brief)}'}],
        None, _chain, fb, role="bull")


def bear_open(brief, _chain=None):
    fb = {"risks": ["rule-fallback: unquantified tail risk"],
          "kill_triggers": ["PF<1.0 over last 20"],
          "confidence": 0.5}
    return _call([
        {"role": "system", "content": SYS},
        {"role": "user", "content":
         f"Argue AGAINST deploying a grid bot here. Schema: "
         f'{{"risks":[str],"kill_triggers":[str],"confidence":0-1}}. '
         f'Candidate: {brief_text(brief)}{_memory_sentence(brief)}'}],
        None, _chain, fb, role="bear")


def rebuttal(brief, own, other, stance, _chain=None):
    fb = {"refined": f"rule-fallback {stance} stands", "concedes": [],
          "confidence": 0.5}
    role = "bull_rebuttal" if stance == "bullish" else "bear_rebuttal"
    return _call([
        {"role": "system", "content": SYS},
        {"role": "user", "content":
         f"You are the {stance} debater. Your opening: {json.dumps(own)}. "
         f"Opponent: {json.dumps(other)}. Respond with schema "
         f'{{"refined":str,"concedes":[str],"confidence":0-1}}. '
         f"Candidate: {brief_text(brief)}"}],
        None, _chain, fb, role=role)


def facilitator(brief, bull, bear, _chain=None):
    fb = {"decision": "GO" if brief.get("score_final", 0) > 45 else "NO_GO",
          "grid_type": GRID_TYPE.get(brief.get("regime"), "neutral"),
          "rationale": "rule-fallback: score gate", "confidence": 0.5}
    return _call([
        {"role": "system", "content": SYS},
        {"role": "user", "content":
         f"Pick the prevailing side. Bull: {json.dumps(bull)}. "
         f"Bear: {json.dumps(bear)}. Schema: "
         f'{{"decision":"GO|NO_GO","grid_type":"long|short|neutral",'
         f'"rationale":str,"confidence":0-1}}. Candidate: '
         f'{brief_text(brief)}{_memory_sentence(brief)}'}],
        None, _chain, fb, role="facilitator")


def risk_review(brief, ticket, stance, _chain=None):
    fb = {"approve": True,
          "max_alloc_mult": 1.0 if stance != "conservative" else 0.5,
          "step_mult": 1.0, "notes": f"rule-fallback {stance}",
          "veto_reason": None}
    if stance == "conservative" and brief.get("venue") == "binance" \
            and ticket.get("grid_type") == "short":
        fb = {"approve": False, "max_alloc_mult": 0.0, "step_mult": 1.0,
              "notes": "rule-fallback", "veto_reason": "spot cannot short"}
        return fb, True
    role = {"seeking": "risk_seeking", "neutral": "risk_neutral",
            "conservative": "risk_conservative"}.get(stance)
    return _call([
        {"role": "system", "content": SYS},
        {"role": "user", "content":
         f"You are the {stance} risk manager. Ticket: {json.dumps(ticket)}. "
         f"Schema: {{\"approve\":bool,\"max_alloc_mult\":0-1,"
         f'"step_mult":0.5-2,"notes":str,"veto_reason":str|null}}. '
         f"Candidate: {brief_text(brief)}"}],
        None, _chain, fb, role=role)


def deliberate(brief, _chain=None, debate_rounds=1):
    """Full pipeline → trade_ticket dict (or veto). Max 5+3=8 LLM calls.

    On total LLM outage every stage falls back to its rule-default and
    llm_degraded is set True; the rotator still gets a usable ticket.
    """
    degraded = False
    bull, d = bull_open(brief, _chain)
    degraded |= d
    bear, d = bear_open(brief, _chain)
    degraded |= d
    bull_open0, bear_open0 = dict(bull), dict(bear)
    for _ in range(max(debate_rounds, 0)):
        bull, d = rebuttal(brief, bull, bear, "bullish", _chain)
        degraded |= d
        bear, d = rebuttal(brief, bear, bull, "bearish", _chain)
        degraded |= d
        break
    verdict, d = facilitator(brief, bull, bear, _chain)
    degraded |= d
    ticket = {
        "symbol": brief.get("symbol"), "venue": brief.get("venue"),
        "tv_symbol": brief.get("tv_symbol"),
        "regime": brief.get("regime"),
        "decision": verdict.get("decision", "NO_GO"),
        "grid_type": verdict.get("grid_type",
                                GRID_TYPE.get(brief.get("regime"), "neutral")),
        "rationale": verdict.get("rationale", ""),
        "confidence": verdict.get("confidence", 0.5),
        "debate": {"bull": bull, "bear": bear,
                   "bull_open": bull_open0, "bear_open": bear_open0},
        "facilitator_llm": verdict.get("_llm"),
        "llm_degraded": degraded,
    }
    if ticket["decision"] != "GO":
        ticket["veto"] = "facilitator NO_GO"
        return ticket
    risks = {}
    for stance in ("seeking", "neutral", "conservative"):
        r, d = risk_review(brief, ticket, stance, _chain)
        degraded |= d
        risks[stance] = r
    ticket["llm_degraded"] = degraded
    ticket["risk"] = risks
    vetoes = [f"{s}: {r['veto_reason']}" for s, r in risks.items()
              if not r.get("approve", True)]
    if vetoes:
        ticket["decision"] = "NO_GO"
        ticket["veto"] = "; ".join(vetoes)
        return ticket
    ticket["max_alloc_mult"] = min(r.get("max_alloc_mult", 1.0)
                                   for r in risks.values())
    steps = [max(r.get("step_mult", 1.0), 0.1) for r in risks.values()]
    ticket["step_mult"] = math.exp(sum(math.log(s) for s in steps) / len(steps))
    ticket["risk_notes"] = [r.get("notes", "") for r in risks.values()]
    return ticket


# -- public surface -------------------------------------------------------

# `swarm` is the legacy alias used by the daemon port; expose it as an
# attribute of this module so existing tests/usages keep working.
swarm = sys.modules[__name__]
swarm_ticket = deliberate  # explicit name for the rotator's contract


if __name__ == "__main__":
    demo = {"symbol": "PUMP", "venue": "hyperliquid",
            "tv_symbol": "BINANCE:PUMPUSDT",
            "regime": "chop_high_volatility", "score_final": 111.0,
            "step": 1.087, "spread_pct": 0.046,
            "metrics": {"price": 1.0, "atr_pct": 2.174, "adx14": 18.7,
                        "rsi14": 52.8, "bb_width_pctile": 52.6},
            "confluence_notes": ["squeeze-fires"], "evidence": {},
            "slot": {"slot": 1, "balance": 125.0, "max_commitment": 62.5}}
    print(json.dumps(deliberate(demo), indent=2))