"""grid.agents — autonomous decision modules for the standalone grid fleet.

Ported from grid-autonomy (execution target = this workspace's FT fleet
under grid/state/ft_fleet/). Modules:

  llm      LLM provider chain (CF primary, NVIDIA/OpenRouter/Mistral
           fallback). Stdlib-only urllib, env-driven creds, pluggable
           per-role pinning via GRID_LLM_ROLES.
  deliber  TradingAgents-pattern swarm (bull/bear/(rebuttal)/facilitator/
           risk) for one rotation candidate. Returns a GO/NO_GO ticket
           with sizing/geometry hints. Rule-fallback on total LLM outage —
           never blocks the loop.

Auth: keys come from env only (MISTRAL_API_KEY, OPENROUTER_API_KEY, …).
Never logged, never written to disk.

See AGENTS.md §"The grid-autonomy live runtime" for the daemon-side loop
this repo is re-homing; grid-autonomy remains owned by other agents and
is never edited.
"""
from .llm import (  # noqa: F401
    chat, chat_json, ping, _providers, _has_creds, _extract_json,
    CF_MODEL_DEFAULT, NVIDIA_MODEL_DEFAULT, OPENROUTER_MODEL_DEFAULT,
    MISTRAL_MODEL_DEFAULT, ROLE_KEYS,
)
from .deliber import (  # noqa: F401
    deliberate, brief_text, bull_open, bear_open, rebuttal, facilitator,
    risk_review, swarm_ticket, GRID_TYPE,
)