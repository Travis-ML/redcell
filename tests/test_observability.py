from redcell.observability import Hooks


def test_hooks_are_noop_by_default():
    h = Hooks()
    # Should not raise when no callbacks registered.
    h.emit("llm_start", model="x")
    h.emit("tool_end", name="t", duration_ms=1.0)


def test_hooks_invoke_registered_callback():
    seen = []
    h = Hooks()
    h.on("tool_start", lambda **kw: seen.append(kw))
    h.emit("tool_start", name="calc", args={"a": 1})
    assert seen == [{"name": "calc", "args": {"a": 1}}]
