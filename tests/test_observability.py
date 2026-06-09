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


def test_configure_logging_json_to_file(tmp_path):
    import json
    import logging

    import structlog

    from redcell.observability import configure_logging

    log_path = tmp_path / "events.jsonl"
    configure_logging("INFO", json_logs=True, log_file=str(log_path))
    structlog.get_logger("redcell").info("tool_end", run_id="abc123", name="shell", result="pong")
    logging.shutdown()

    lines = [ln for ln in log_path.read_text().splitlines() if ln.strip()]
    record = json.loads(lines[-1])
    assert record["event"] == "tool_end"
    assert record["run_id"] == "abc123"
    assert record["result"] == "pong"
    # Reset logging so the JSON FileHandler doesn't leak into other tests.
    configure_logging("INFO")
