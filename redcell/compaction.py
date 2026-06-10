"""Context compaction: keep a long run within a small local-model window.

redcell drives local models (vLLM/Ollama) whose context windows are often small
(8k–32k), so an unbounded message history will eventually overflow mid-run. This
module provides the pieces the agent loop uses to stay under budget:

- :func:`estimate_tokens` — a tokenizer-free rough estimate (~4 chars/token) that
  works for any LiteLLM backend.
- :func:`microcompact` — the cheap first tier: blank out the *bodies* of old
  tool results (file dumps, scans) while keeping the message skeleton, no LLM call.
- :func:`safe_split_index` — split history into a prefix-to-summarize and a recent
  tail to keep verbatim, **without orphaning** a ``tool`` result from the assistant
  ``tool_calls`` turn that produced it (local models hard-error on that).
- :func:`build_compacted` — assemble ``[system?, summary, *tail]`` from a summary.
- :data:`COMPACT_PROMPT` — the structured summarization instruction.
- :func:`is_context_overflow` — recognize a provider "prompt too long" error so the
  agent can compact and retry reactively when the estimate was wrong.
"""

from __future__ import annotations

_CHARS_PER_TOKEN = 4
_MSG_OVERHEAD_TOKENS = 4
_CLEARED_MARKER = "[old tool output cleared]"

COMPACT_PROMPT = """\
Summarize the conversation so far so it can continue with the detail below \
preserved but the token count reduced. Write only the summary, no preamble. Cover:

1. Task & intent: what the user is trying to accomplish, in their own words.
2. Targets & environment: hosts, services, paths, credentials, or data discovered.
3. Actions taken: tools/commands run and their key results (exit codes, findings).
4. Errors & blockers hit, and how they were resolved or not.
5. Current state: what was just happening and what remains to be done next.

Be specific and factual — quote exact identifiers, do not invent anything."""


def estimate_message_tokens(message: dict) -> int:
    """Rough token estimate for one message (content + any tool calls)."""
    content = message.get("content") or ""
    chars = len(content) if isinstance(content, str) else len(str(content))
    for tc in message.get("tool_calls") or []:
        chars += len(str(tc.get("function", "")))
    return chars // _CHARS_PER_TOKEN + _MSG_OVERHEAD_TOKENS


def estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate for a whole message list."""
    return sum(estimate_message_tokens(m) for m in messages)


def microcompact(messages: list[dict], keep_recent: int = 3) -> list[dict]:
    """Blank out the bodies of all but the last ``keep_recent`` tool results.

    Cheap, no-LLM tier: the message skeleton (roles, tool_call_ids) is preserved
    so tool pairing stays intact, but large stale outputs stop costing tokens.
    Returns a new list; the input is not mutated.
    """
    tool_positions = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    clear = set(tool_positions[:-keep_recent]) if keep_recent >= 0 else set(tool_positions)
    out: list[dict] = []
    for i, m in enumerate(messages):
        if i in clear and m.get("content") != _CLEARED_MARKER:
            out.append({**m, "content": _CLEARED_MARKER})
        else:
            out.append(m)
    return out


def _snap_to_safe(messages: list[dict], idx: int) -> int:
    """Move ``idx`` earlier until the tail ``messages[idx:]`` is self-consistent.

    Every ``tool`` message in the tail must have its producing assistant
    ``tool_calls`` turn in the tail too, or the next API call orphans it.
    """
    while idx > 0:
        needed = {
            m.get("tool_call_id")
            for m in messages[idx:]
            if m.get("role") == "tool" and m.get("tool_call_id")
        }
        provided = {tc.get("id") for m in messages[idx:] for tc in (m.get("tool_calls") or [])}
        if needed <= provided:
            return idx
        idx -= 1
    return idx


def safe_split_index(messages: list[dict], keep_recent_tokens: int) -> int:
    """Index splitting history into prefix (to summarize) and tail (kept verbatim).

    The tail accumulates from the end until it holds ~``keep_recent_tokens``, then
    the boundary is snapped earlier so it never orphans a tool result. Returns 0
    when nothing can be safely summarized (caller should skip full compaction).
    """
    total = 0
    idx = 0
    for i in range(len(messages) - 1, -1, -1):
        total += estimate_message_tokens(messages[i])
        if total >= keep_recent_tokens:
            idx = i
            break
    return _snap_to_safe(messages, idx)


def build_compacted(messages: list[dict], summary_text: str, split_idx: int) -> list[dict]:
    """Assemble ``[system?, summary, *tail]`` keeping any leading system message."""
    head: list[dict] = []
    if messages and messages[0].get("role") == "system":
        head = [messages[0]]
        split_idx = max(split_idx, 1)
    summary = {
        "role": "user",
        "content": f"[Earlier conversation summarized to save context]\n{summary_text}",
    }
    return [*head, summary, *messages[split_idx:]]


def is_context_overflow(exc: Exception) -> bool:
    """True if ``exc`` is a provider 'prompt/context too long' error."""
    if type(exc).__name__ == "ContextWindowExceededError":
        return True
    text = str(exc).lower()
    return ("context" in text or "prompt" in text) and (
        "too long" in text or "maximum" in text or "exceed" in text or "window" in text
    )
