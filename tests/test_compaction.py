"""Tests for context compaction: estimation, microcompact, safe split."""

from redcell.compaction import (
    build_compacted,
    estimate_tokens,
    is_context_overflow,
    microcompact,
    safe_split_index,
)


def test_estimate_tokens_grows_with_content():
    short = [{"role": "user", "content": "hi"}]
    long = [{"role": "user", "content": "x" * 4000}]
    assert estimate_tokens(long) > estimate_tokens(short)


def test_microcompact_clears_old_tool_bodies_keeps_recent():
    messages = [
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "a", "content": "OLD-1" * 100},
        {"role": "tool", "tool_call_id": "b", "content": "OLD-2" * 100},
        {"role": "tool", "tool_call_id": "c", "content": "RECENT"},
    ]
    out = microcompact(messages, keep_recent=1)
    assert out[1]["content"] == "[old tool output cleared]"
    assert out[2]["content"] == "[old tool output cleared]"
    assert out[3]["content"] == "RECENT"  # last tool kept
    # roles / ids preserved (pairing intact)
    assert [m["role"] for m in out] == ["user", "tool", "tool", "tool"]
    assert messages[1]["content"] != "[old tool output cleared]"  # input not mutated


def test_safe_split_does_not_orphan_tool_result():
    # assistant tool_calls turn (idx 1) must not be split from its tool result (idx 2)
    messages = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "r"},
        {"role": "assistant", "content": "done"},
    ]
    # Ask to keep a tiny tail; the split must still not leave the tool msg orphaned.
    idx = safe_split_index(messages, keep_recent_tokens=1)
    tail = messages[idx:]
    needed = {m["tool_call_id"] for m in tail if m.get("role") == "tool"}
    provided = {tc["id"] for m in tail for tc in m.get("tool_calls", [])}
    assert needed <= provided  # every tail tool result has its producing turn


def test_build_compacted_keeps_system_and_inserts_summary():
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "older"},
        {"role": "user", "content": "recent"},
    ]
    out = build_compacted(messages, "A SUMMARY", split_idx=3)
    assert out[0] == {"role": "system", "content": "SYS"}  # system preserved
    assert "A SUMMARY" in out[1]["content"]  # summary inserted
    assert out[-1] == {"role": "user", "content": "recent"}  # tail kept


def test_is_context_overflow_detection():
    assert is_context_overflow(type("ContextWindowExceededError", (Exception,), {})())
    assert is_context_overflow(Exception("This model's maximum context length is 8192 tokens"))
    assert not is_context_overflow(Exception("rate limit exceeded"))
