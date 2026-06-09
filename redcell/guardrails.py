"""Input/output guardrails: a small pluggable moderation layer.

The agent calls a :class:`Guardrail` to screen the user's input before a run, the
model's text after it, and every tool result before it reaches the model. The
shipped :class:`PatternGuardrail` is a dependency-free, deterministic baseline —
it refuses a few high-signal harmful intents and *redacts* PII and
internal-architecture strings from output and tool results (the deterministic
half of the fixes for the privacy/pii:session/copyright findings). Each verdict
carries machine-readable ``categories`` for per-category scan scorecards.

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
    ``categories`` lists the machine-readable classes that fired (e.g.
    ``("pii:email", "internal:path")``), so a scan's events aggregate into a
    per-category scorecard instead of opaque log lines.
    """

    allowed: bool
    text: str
    reason: str | None = None
    categories: tuple[str, ...] = ()


@runtime_checkable
class Guardrail(Protocol):
    # Async so a real moderator (llm-guard, Llama Guard, an LLM self-check) can do
    # network I/O without blocking the agent's event loop. Pure in-process
    # implementations like PatternGuardrail just don't await anything.
    async def check_input(self, text: str) -> Verdict: ...
    async def check_output(self, text: str) -> Verdict: ...


class NullGuardrail:
    """A no-op guardrail: allows everything, redacts nothing (vulnerable mode)."""

    async def check_input(self, text: str) -> Verdict:
        return Verdict(allowed=True, text=text)

    async def check_output(self, text: str) -> Verdict:
        return Verdict(allowed=True, text=text)


# High-signal harmful-intent patterns, each tagged with a machine-readable
# category. Coarse by design — the safety system prompt is the semantic layer;
# this is a deterministic backstop.
_BLOCK_INPUT = [
    ("fraud:fake_invoice", re.compile(r"\bfake\s+invoice", re.I)),
    (
        "pii:card_scrape",
        re.compile(r"(extract|scrape|harvest|steal).{0,40}(credit\s*card|card\s*number|cvv)", re.I),
    ),
    ("copyright:lyrics", re.compile(r"(complete|full)\s+lyrics", re.I)),
    (
        "secrets:fs_scan",
        re.compile(
            r"(scan|search).{0,40}(/home|/tmp|/var)\b.{0,40}(password|api[\s_-]?key|secret)", re.I
        ),
    ),
]


def _luhn_ok(candidate: str) -> bool:
    """True if the digit run passes the Luhn checksum — i.e. looks like a real card.

    Gating card redaction on Luhn kills the dominant false positive: long but
    arbitrary digit runs (order ids, hashes, timestamps) that aren't payment cards.
    """
    digits = [int(c) for c in candidate if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


# Output/tool-result redaction, each rule a (category, pattern, predicate). When
# the predicate is set, a match is redacted only if it returns True — used to gate
# card redaction on Luhn so arbitrary long digit runs aren't mistaken for cards.
# PII first, then internal-architecture identifiers the scan caught us disclosing.
_REDACT: list[tuple[str, re.Pattern[str], object]] = [
    ("pii:email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), None),
    # 13–19 digits, optionally grouped by single spaces/hyphens; Luhn-gated.
    ("pii:card", re.compile(r"\b\d(?:[ -]?\d){12,18}\b"), _luhn_ok),
    ("pii:ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), None),
    # Phone: require the (xxx) xxx-xxxx / xxx-xxx-xxxx shape with separators, so a
    # bare long digit run no longer matches. Optional +country and leading 1.
    (
        "pii:phone",
        re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}(?!\d)"),
        None,
    ),
    ("internal:path", re.compile(r"/home/redcell\S*"), None),
    (
        "internal:identifier",
        re.compile(r"\b(qdrant|fastembed|agentgateway|vllm|mcp-server[\w-]*|redcell-kb)\b", re.I),
        None,
    ),
]


def make_guardrail(enabled: bool) -> Guardrail:
    """Return the active guardrail: :class:`PatternGuardrail` on, else no-op."""
    return PatternGuardrail() if enabled else NullGuardrail()


class PatternGuardrail:
    """Deterministic baseline guardrail (no external dependencies)."""

    async def check_input(self, text: str) -> Verdict:
        for category, pat in _BLOCK_INPUT:
            if pat.search(text):
                return Verdict(
                    allowed=False, text=REFUSAL, reason="blocked_input", categories=(category,)
                )
        return Verdict(allowed=True, text=text)

    async def check_output(self, text: str) -> Verdict:
        redacted = text
        fired: list[str] = []
        for category, pat, predicate in _REDACT:
            if predicate is None:
                redacted, n = pat.subn(REDACTED, redacted)
            else:
                # Redact a match only when the predicate accepts it (e.g. Luhn).
                hits = 0

                def _sub(m: re.Match[str], _pred=predicate) -> str:
                    nonlocal hits
                    if _pred(m.group()):
                        hits += 1
                        return REDACTED
                    return m.group()

                redacted = pat.sub(_sub, redacted)
                n = hits
            if n:
                fired.append(category)
        if fired:
            return Verdict(allowed=True, text=redacted, reason="redacted", categories=tuple(fired))
        return Verdict(allowed=True, text=text)
