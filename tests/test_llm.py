from redcell.llm import (
    LLM,
    LLMResponse,
    ToolCall,
    _parse_response,
    backoff_delay,
    is_retryable,
    retry_after_seconds,
)


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


class _Err(Exception):
    """Fake provider error carrying an optional status code / response."""

    def __init__(self, name, status=None, headers=None, retry_after=None):
        super().__init__(name)
        self.__class__.__name__ = name  # mimic litellm's exception class names
        if status is not None:
            self.status_code = status
        if headers is not None or status is not None:
            self.response = type("R", (), {"status_code": status, "headers": headers or {}})()
        if retry_after is not None:
            self.retry_after = retry_after


def test_is_retryable_by_status_code():
    assert is_retryable(_Err("X", status=429))
    assert is_retryable(_Err("X", status=503))
    assert is_retryable(_Err("X", status=408))
    assert not is_retryable(_Err("X", status=400))
    assert not is_retryable(_Err("X", status=401))


def test_is_retryable_by_exception_name():
    assert is_retryable(_Err("RateLimitError"))
    assert is_retryable(_Err("APIConnectionError"))
    assert not is_retryable(_Err("AuthenticationError"))
    assert not is_retryable(_Err("ContextWindowExceededError"))
    assert not is_retryable(_Err("SomeUnknownError"))  # unknown = fail fast


def test_retry_after_prefers_attribute_then_header():
    assert retry_after_seconds(_Err("X", retry_after=7)) == 7.0
    assert retry_after_seconds(_Err("X", status=429, headers={"retry-after": "3"})) == 3.0
    http_date = _Err("X", status=429, headers={"Retry-After": "Wed, 21 Oct"})
    assert retry_after_seconds(http_date) is None  # HTTP-date form ignored
    assert retry_after_seconds(_Err("X")) is None


def test_backoff_is_exponential_and_bounded():
    # Lower bound is the capped exponential; jitter only adds (≤25% of base).
    assert 1.0 <= backoff_delay(1, base=1.0, cap=30.0) <= 1.25
    assert 2.0 <= backoff_delay(2, base=1.0, cap=30.0) <= 2.25
    assert 30.0 <= backoff_delay(10, base=1.0, cap=30.0) <= 30.25  # capped


async def test_complete_retries_transient_then_succeeds(monkeypatch):
    calls = {"n": 0}

    async def flaky(self, kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _Err("RateLimitError", status=429)
        return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    monkeypatch.setattr(LLM, "_acompletion", flaky)
    # base/max delay 0 -> no real sleeping.
    llm = LLM("m", max_retries=5, retry_base_delay=0.0, retry_max_delay=0.0)
    resp = await llm.complete([{"role": "user", "content": "hi"}])
    assert resp.text == "ok"
    assert calls["n"] == 3  # failed twice, succeeded on the third


async def test_complete_does_not_retry_non_transient(monkeypatch):
    calls = {"n": 0}

    async def boom(self, kwargs):
        calls["n"] += 1
        raise _Err("BadRequestError", status=400)

    monkeypatch.setattr(LLM, "_acompletion", boom)
    llm = LLM("m", max_retries=5, retry_base_delay=0.0, retry_max_delay=0.0)
    try:
        await llm.complete([{"role": "user", "content": "hi"}])
        raise AssertionError("expected the error to propagate")
    except _Err:
        pass
    assert calls["n"] == 1  # no retries on a 400


async def test_complete_gives_up_after_max_retries(monkeypatch):
    calls = {"n": 0}

    async def always_429(self, kwargs):
        calls["n"] += 1
        raise _Err("RateLimitError", status=429)

    monkeypatch.setattr(LLM, "_acompletion", always_429)
    llm = LLM("m", max_retries=2, retry_base_delay=0.0, retry_max_delay=0.0)
    try:
        await llm.complete([{"role": "user", "content": "hi"}])
        raise AssertionError("expected the error to propagate")
    except _Err:
        pass
    assert calls["n"] == 3  # 1 initial + 2 retries


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
