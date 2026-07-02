"""LiteLLMProvider — model-ladder completions over LiteLLM (implements core.LLM).

One interface across every provider: the caller passes a model string and LiteLLM routes it
(Bedrock / Azure OpenAI / Gemini / Anthropic). That pluggability is the eval story — the
Phase 4 harness reports completion/test-pass/recovery *per model* — and it keeps the cloud
provider non-load-bearing: `bedrock/...` is the documented default, but `azure/<deployment>`
(+ AZURE_API_* env) or any other model string is a config change, not a code change.

`drop_params=True` lets a single call site target models with different capabilities — e.g.
Opus 4.7+ rejects `temperature`, so LiteLLM silently drops it rather than erroring.
"""

from __future__ import annotations

import logging

import litellm

from portage_agent.config import settings
from portage_agent.core.interfaces import LLMMessage, LLMResponse

log = logging.getLogger("portage.llm")

# LiteLLM is chatty about provider internals at INFO; keep our logs readable.
litellm.suppress_debug_info = True


class LiteLLMProvider:
    """core.LLM adapter. Stateless; the model is chosen per call (default = driver tier)."""

    def __init__(self, *, default_model: str | None = None) -> None:
        self.default_model = default_model or settings.llm_driver_model

    async def complete(
        self, messages: list[LLMMessage], *, model: str | None = None, **kwargs: object
    ) -> LLMResponse:
        model = model or self.default_model
        params: dict = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": settings.llm_max_tokens,
            "timeout": settings.llm_timeout_seconds,
            "num_retries": settings.llm_request_max_retries,
            "drop_params": True,
        }
        if settings.llm_temperature is not None:
            params["temperature"] = settings.llm_temperature
        params.update(kwargs)

        log.info("llm complete | model=%s messages=%s", model, len(messages))
        resp = await litellm.acompletion(**params)

        text = (resp.choices[0].message.content or "").strip()
        usage = getattr(resp, "usage", None)
        return LLMResponse(
            text=text,
            model=getattr(resp, "model", model) or model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )
