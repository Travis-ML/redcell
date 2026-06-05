from redcell.llm import LLM, LLMResponse, ToolCall, _parse_response


def test_parse_response_text_only():
    raw = {
        "choices": [{"message": {"content": "hello", "tool_calls": None}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }
    parsed = _parse_response(raw)
    assert isinstance(parsed, LLMResponse)
    assert parsed.text == "hello"
    assert parsed.tool_calls == []
    assert parsed.usage["total_tokens"] == 4


def test_parse_response_with_tool_calls():
    raw = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "add", "arguments": '{"a": 1, "b": 2}'},
                        }
                    ],
                }
            }
        ],
        "usage": {},
    }
    parsed = _parse_response(raw)
    assert parsed.tool_calls == [ToolCall(id="call_1", name="add", arguments={"a": 1, "b": 2})]


def test_parse_response_captures_reasoning_channel():
    # vLLM with a reasoning parser (e.g. gemma4) splits thinking into its own
    # channel; LiteLLM normalizes it to ``reasoning_content``. The message
    # content holds the actual answer and must not be polluted by reasoning.
    raw = {
        "choices": [
            {
                "message": {
                    "content": "The ball costs $0.05.",
                    "reasoning_content": "Let x be the ball. x + (x+1) = 1.10 ...",
                    "tool_calls": None,
                }
            }
        ],
        "usage": {},
    }
    parsed = _parse_response(raw)
    assert parsed.text == "The ball costs $0.05."
    assert parsed.reasoning == "Let x be the ball. x + (x+1) = 1.10 ..."
    assert parsed.tool_calls == []


def test_parse_response_reasoning_with_tool_calls():
    # A reasoning model may emit thinking AND a tool call while ``content`` is
    # null. Tool calls must still be pulled and the reasoning preserved.
    raw = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "reasoning_content": "I should call the add tool.",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "add", "arguments": '{"a": 21, "b": 21}'},
                        }
                    ],
                }
            }
        ],
        "usage": {},
    }
    parsed = _parse_response(raw)
    assert parsed.text is None
    assert parsed.reasoning == "I should call the add tool."
    assert parsed.tool_calls == [ToolCall(id="call_1", name="add", arguments={"a": 21, "b": 21})]


def test_parse_response_reasoning_raw_field_fallback():
    # Some vLLM builds expose the channel as ``reasoning`` (pre-normalization).
    raw = {"choices": [{"message": {"content": "hi", "reasoning": "thinking..."}}], "usage": {}}
    parsed = _parse_response(raw)
    assert parsed.reasoning == "thinking..."


async def test_llm_complete_calls_litellm(monkeypatch):
    captured = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return {
            "choices": [{"message": {"content": "ok", "tool_calls": None}}],
            "usage": {"total_tokens": 2},
        }

    monkeypatch.setattr("redcell.llm.litellm.acompletion", fake_acompletion)
    llm = LLM(model="anthropic/claude-opus-4-8", temperature=0.1, max_tokens=64)
    resp = await llm.complete(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert resp.text == "ok"
    assert captured["model"] == "anthropic/claude-opus-4-8"
    assert captured["temperature"] == 0.1
    assert "tools" not in captured  # empty tools list omitted
