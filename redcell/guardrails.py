"""Input/output guardrails: a small pluggable moderation layer.

The agent calls a :class:`Guardrail` to screen the user's input before a run and
the model's text after it. The shipped :class:`PatternGuardrail` is a dependency
-free, deterministic baseline — it refuses a few high-signal harmful intents and
*redacts* PII and internal-architecture strings from output (the deterministic
half of the fixes for the privacy/pii:session/copyright findings).

It is intentionally a baseline, not a complete moderator: semantic categories
(bias, fraud framing, hallucination) are handled by the safety system prompt in
:mod:`redcell.prompts`. For production-grade semantic moderation, implement this
same :class:`Guardrail` protocol over llm-guard, Llama Guard, or an LLM self-check
and pass it to the :class:`~redcell.agent.Agent` — no other code changes needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

REFUSAL = "I can't help with that request."
REDACTED = "[redacted]"


@dataclass
class Verdict:
    """Outcome of a guardrail check.

    ``allowed`` is False only for a blocked *input*. ``text`` is the text to use
    going forward: the refusal message for a blocked input, or the (possibly
    redacted) text for output. ``reason`` is a short tag for logging.
    """

    allowed: bool
    text: str
    reason: str | None = None


@runtime_checkable
class Guardrail(Protocol):
    def check_input(self, text: str) -> Verdict: ...
    def check_output(self, text: str) -> Verdict: ...


class NullGuardrail:
    """A no-op guardrail: allows everything, redacts nothing (vulnerable mode)."""

    def check_input(self, text: str) -> Verdict:
        return Verdict(allowed=True, text=text)

    def check_output(self, text: str) -> Verdict:
        return Verdict(allowed=True, text=text)


# High-signal harmful-intent patterns. Coarse by design — the safety system
# prompt is the semantic layer; this is a deterministic backstop.
_BLOCK_INPUT = [
    re.compile(r"\bfake\s+invoice", re.I),
    re.compile(r"(extract|scrape|harvest|steal).{0,40}(credit\s*card|card\s*number|cvv)", re.I),
    re.compile(r"(complete|full)\s+lyrics", re.I),
    re.compile(
        r"(scan|search).{0,40}(/home|/tmp|/var)\b.{0,40}(password|api[\s_-]?key|secret)", re.I
    ),
]

# Output redaction. PII first, then internal-architecture identifiers that the
# scan caught us disclosing (backend names, sandbox path, model/infra).
_PII = [
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),  # email
    re.compile(r"\b(?:\d[ -]?){13,16}\b"),  # card-like digit runs
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # US SSN
    re.compile(r"\+?\d[\d\s().-]{7,}\d"),  # phone-like
]
_INTERNAL = [
    re.compile(r"/home/redcell\S*"),
    re.compile(r"\b(qdrant|fastembed|agentgateway|vllm|mcp-server[\w-]*|redcell-kb)\b", re.I),
]


def make_guardrail(enabled: bool) -> Guardrail:
    """Return the active guardrail: :class:`PatternGuardrail` on, else no-op."""
    return PatternGuardrail() if enabled else NullGuardrail()


class PatternGuardrail:
    """Deterministic baseline guardrail (no external dependencies)."""

    def check_input(self, text: str) -> Verdict:
        for pat in _BLOCK_INPUT:
            if pat.search(text):
                return Verdict(allowed=False, text=REFUSAL, reason="blocked_input")
        return Verdict(allowed=True, text=text)

    def check_output(self, text: str) -> Verdict:
        redacted = text
        for pat in (*_PII, *_INTERNAL):
            redacted = pat.sub(REDACTED, redacted)
        if redacted != text:
            return Verdict(allowed=True, text=redacted, reason="redacted")
        return Verdict(allowed=True, text=text)
