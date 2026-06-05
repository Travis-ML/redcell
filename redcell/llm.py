"""Async LiteLLM wrapper. The ONLY module that imports a provider SDK."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import litellm


@dataclass(frozen=True)
class ToolCall:
    """A single tool call requested by the model."""

    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    """Normalized model response, provider-independent."""

    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    # The model's reasoning/thinking channel, when the provider exposes one
    # separately from ``text`` (e.g. vLLM with a reasoning parser). ``None``
    # for providers that don't split it out.
    reasoning: str | None = None


def _parse_response(raw: dict) -> LLMResponse:
    """Convert a LiteLLM/OpenAI-shaped response into an :class:`LLMResponse`."""
    message = raw["choices"][0]["message"]
    tool_calls: list[ToolCall] = []
    for tc in message.get("tool_calls") or []:
        fn = tc["function"]
        args = fn.get("arguments") or "{}"
        tool_calls.append(
            ToolCall(
                id=tc["id"],
                name=fn["name"],
                arguments=json.loads(args) if isinstance(args, str) else args,
            )
        )
    # LiteLLM normalizes a reasoning channel to ``reasoning_content``; some raw
    # vLLM builds expose it as ``reasoning``. Prefer the normalized name.
    reasoning = message.get("reasoning_content") or message.get("reasoning")
    return LLMResponse(
        text=message.get("content"),
        tool_calls=tool_calls,
        usage=dict(raw.get("usage") or {}),
        reasoning=reasoning,
    )


class LLM:
    """Thin async client over ``litellm.acompletion``.

    Swapping providers is a config change (the ``model`` string); no other
    module needs to know which vendor is in use.
    """

    def __init__(
        self,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        api_base: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        # ``api_base`` points LiteLLM at an OpenAI-compatible endpoint (e.g. a
        # self-hosted vLLM server); ``api_key`` is sent as the bearer token.
        self.api_base = api_base
        self.api_key = api_key

    async def complete(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if tools:
            kwargs["tools"] = tools
        raw = await litellm.acompletion(**kwargs)
        # LiteLLM returns a pydantic-like object; normalize to dict.
        if not isinstance(raw, dict):
            raw = raw.model_dump()
        return _parse_response(raw)
