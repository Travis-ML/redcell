"""Async LiteLLM wrapper. The ONLY module that imports a provider SDK."""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field

import litellm
import structlog

logger = structlog.get_logger("redcell.llm")

# Exception *type names* (matched without importing litellm's class hierarchy, so
# this is robust across litellm versions and testable with fakes). Transient =
# retry; the rest are caller/auth/validation errors that won't change on retry.
_RETRYABLE_NAMES = frozenset(
    {
        "RateLimitError",
        "APIConnectionError",
        "APIConnectionAbortedError",
        "Timeout",
        "APITimeoutError",
        "InternalServerError",
        "ServiceUnavailableError",
        "OverloadedError",
    }
)
_NON_RETRYABLE_NAMES = frozenset(
    {
        "AuthenticationError",
        "BadRequestError",
        "NotFoundError",
        "PermissionDeniedError",
        "ContextWindowExceededError",
        "ContentPolicyViolationError",
        "UnprocessableEntityError",
    }
)


def _status_code(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def is_retryable(exc: Exception) -> bool:
    """True if ``exc`` is a transient API error worth retrying.

    Status code wins when present (retry 408/409/429 and 5xx; never other 4xx);
    otherwise fall back to the exception class name. Unknown errors are NOT
    retried — fail fast rather than hammer a broken endpoint.
    """
    status = _status_code(exc)
    if status is not None:
        if status in (408, 409, 429) or 500 <= status < 600:
            return True
        if 400 <= status < 500:
            return False
    name = type(exc).__name__
    if name in _NON_RETRYABLE_NAMES:
        return False
    return name in _RETRYABLE_NAMES


def retry_after_seconds(exc: Exception) -> float | None:
    """Extract a server-provided Retry-After delay (seconds) if present."""
    ra = getattr(exc, "retry_after", None)
    if ra is not None:
        try:
            return float(ra)
        except (TypeError, ValueError):
            pass
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers:
        val = headers.get("retry-after") or headers.get("Retry-After")
        if val is not None:
            try:
                return float(val)  # numeric seconds; HTTP-date form is ignored
            except (TypeError, ValueError):
                return None
    return None


def backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with capped 25%% jitter: min(base*2^(n-1), cap)+jitter."""
    raw = min(base * (2 ** (attempt - 1)), cap)
    return raw + random.random() * 0.25 * base


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
        *,
        max_retries: int = 5,
        retry_base_delay: float = 0.5,
        retry_max_delay: float = 30.0,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        # ``api_base`` points LiteLLM at an OpenAI-compatible endpoint (e.g. a
        # self-hosted vLLM server); ``api_key`` is sent as the bearer token.
        self.api_base = api_base
        self.api_key = api_key
        # Transient-error retry: a single 429/5xx/connection blip from a local
        # server or cloud should not kill a run. 0 disables retries.
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay

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
        raw = await self._complete_with_retry(kwargs)
        # LiteLLM returns a pydantic-like object; normalize to dict.
        if not isinstance(raw, dict):
            raw = raw.model_dump()
        return _parse_response(raw)

    async def _complete_with_retry(self, kwargs: dict) -> object:
        """Call ``acompletion``, retrying transient errors with backoff."""
        for attempt in range(1, self.max_retries + 2):  # 1 try + max_retries
            try:
                return await self._acompletion(kwargs)
            except Exception as exc:
                if attempt > self.max_retries or not is_retryable(exc):
                    raise
                delay = retry_after_seconds(exc)
                if delay is None:
                    delay = backoff_delay(attempt, self.retry_base_delay, self.retry_max_delay)
                logger.warning(
                    "llm_retry",
                    attempt=attempt,
                    max_retries=self.max_retries,
                    delay=round(delay, 2),
                    error=f"{type(exc).__name__}: {exc}",
                )
                await asyncio.sleep(delay)

    async def _acompletion(self, kwargs: dict) -> object:
        """Seam over ``litellm.acompletion`` (overridable in tests)."""
        return await litellm.acompletion(**kwargs)
