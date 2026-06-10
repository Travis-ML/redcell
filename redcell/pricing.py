"""Per-model token pricing for the scan scorecard.

Rates are USD per **million** tokens. They are approximate and provider list
prices change — treat this table as editable config, not gospel. Models with no
entry (notably local vLLM/Ollama) fall back to a zero rate, so their token counts
are still tallied but cost reads $0; the scorecard flags that a model was unpriced.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rate:
    """USD per million tokens for each token class."""

    input: float
    output: float
    cache_write: float = 0.0
    cache_read: float = 0.0


# Keyed by a substring of the LiteLLM model id (provider prefix is ignored), so
# "anthropic/claude-opus-4-8" matches "claude-opus". First containing key wins.
RATES: dict[str, Rate] = {
    "claude-opus": Rate(15.0, 75.0, 18.75, 1.50),
    "claude-sonnet": Rate(3.0, 15.0, 3.75, 0.30),
    "claude-haiku": Rate(0.80, 4.0, 1.0, 0.08),
    "gpt-4o-mini": Rate(0.15, 0.60),
    "gpt-4o": Rate(2.50, 10.0),
    "gpt-4.1-mini": Rate(0.40, 1.60),
    "gpt-4.1": Rate(2.0, 8.0),
}

# Unknown / local models: tokens still counted, cost contributes $0.
DEFAULT_RATE = Rate(0.0, 0.0, 0.0, 0.0)


def _rate_for(model: str) -> Rate | None:
    needle = (model or "").lower()
    for key, rate in RATES.items():
        if key in needle:
            return rate
    return None


def normalize_usage(usage: dict | None) -> dict:
    """Normalize an OpenAI- or Anthropic-shaped usage dict to token classes.

    Returns ``{input, output, cache_read, cache_write}``. OpenAI's
    ``prompt_tokens`` *includes* cached tokens, so cached are subtracted out to
    avoid double-charging; Anthropic's ``input_tokens`` already excludes them.
    """
    u = usage or {}
    output = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
    cache_write = int(u.get("cache_creation_input_tokens") or 0)
    cache_read = int(u.get("cache_read_input_tokens") or 0)
    details = u.get("prompt_tokens_details")
    if not cache_read and isinstance(details, dict):
        cache_read = int(details.get("cached_tokens") or 0)

    if "prompt_tokens" in u:  # OpenAI: prompt includes cached -> subtract
        input_ = max(0, int(u.get("prompt_tokens") or 0) - cache_read)
    else:  # Anthropic: input_tokens already excludes cache
        input_ = int(u.get("input_tokens") or 0)
    return {"input": input_, "output": output, "cache_read": cache_read, "cache_write": cache_write}


def cost_usd(model: str, usage: dict | None) -> tuple[float, bool]:
    """Return ``(usd_cost, is_unknown_model)`` for one completion's usage."""
    rate = _rate_for(model)
    unknown = rate is None
    rate = rate or DEFAULT_RATE
    n = normalize_usage(usage)
    cost = (
        n["input"] * rate.input
        + n["output"] * rate.output
        + n["cache_read"] * rate.cache_read
        + n["cache_write"] * rate.cache_write
    ) / 1_000_000
    return cost, unknown
