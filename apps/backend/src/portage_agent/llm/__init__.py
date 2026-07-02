"""LLM — LiteLLM model ladder (implements core.LLM). Wired in Phase 2.

`get_llm()` is the single construction point so the rest of the agent never imports a
provider directly. Model escalation (driver→Opus on repeated failure) lands in Phase 3; for
now `get_llm()` returns the driver-tier provider, with the model still overridable per call.
"""

from __future__ import annotations

from portage_agent.core.interfaces import LLM

from .litellm_provider import LiteLLMProvider

__all__ = ["LiteLLMProvider", "get_llm"]


def get_llm() -> LLM:
    """Return the configured LLM provider (driver tier by default)."""
    return LiteLLMProvider()
