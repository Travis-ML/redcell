"""Tests for the OpenAI-compatible HTTP server. Fully offline via StubLLM."""

import json

import pytest
from fastapi.testclient import TestClient

from redcell.agent import Agent
from redcell.llm import LLMResponse, ToolCall
from redcell.server import create_app
from redcell.sessions import SessionStore
from redcell.tools import Tool, ToolRegistry, tool
from tests.conftest import StubLLM


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


def make_client(scripted, tools=None, **app_kwargs):
    """Build a TestClient whose agent replays ``scripted`` per request."""

    def factory():
        return Agent(llm=StubLLM(list(scripted)), tools=tools or ToolRegistry())

    return TestClient(create_app(factory, model_id="redcell", **app_kwargs))


def test_list_models():
    client = make_client([LLMResponse(text="x")])
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert "redcell" in ids
    assert body["data"][0]["object"] == "model"


def test_chat_completion_non_stream_text_and_reasoning():
    client = make_client(
        [LLMResponse(text="hello", reasoning="because", usage={"total_tokens": 5})]
    )
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "redcell", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    choice = body["choices"][0]
    assert choice["message"]["content"] == "hello"
    assert choice["message"]["reasoning_content"] == "because"
    assert choice["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] == 5


def test_chat_completion_runs_tool_loop():
    scripted = [
        LLMResponse(
            text=None, tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 2, "b": 3})]
        ),
        LLMResponse(text="The answer is 5"),
    ]
    client = make_client(scripted, tools=ToolRegistry([add]))
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "redcell", "messages": [{"role": "user", "content": "2+3?"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "The answer is 5"


def test_chat_completion_streaming():
    client = make_client([LLMResponse(text="streamed", reasoning="r")])
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "redcell",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    # Collect the SSE payloads (lines starting with "data: ").
    payloads = []
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            payloads.append(line[len("data: ") :])
    assert payloads[-1] == "[DONE]"

    chunks = [json.loads(p) for p in payloads if p != "[DONE]"]
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    # Reassembled content deltas reproduce the final answer.
    content = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert content == "streamed"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_auth_required_when_key_set():
    scripted = [LLMResponse(text="ok")]
    client = make_client(scripted, api_key="secret")

    # No token -> 401.
    assert (
        client.post(
            "/v1/chat/completions",
            json={"model": "redcell", "messages": [{"role": "user", "content": "hi"}]},
        ).status_code
        == 401
    )

    # Wrong token -> 401.
    assert (
        client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer nope"},
            json={"model": "redcell", "messages": [{"role": "user", "content": "hi"}]},
        ).status_code
        == 401
    )

    # Right token -> 200.
    assert (
        client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer secret"},
            json={"model": "redcell", "messages": [{"role": "user", "content": "hi"}]},
        ).status_code
        == 200
    )


def test_auth_open_by_default():
    client = make_client([LLMResponse(text="ok")])
    # No api_key configured -> any/no token allowed.
    assert client.get("/v1/models").status_code == 200


class _FakeGateway:
    def __init__(self):
        self.started = self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class _FakeManager:
    def __init__(self, tools=None):
        self._tools = tools or []
        self.entered = self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.exited = True

    def tools(self):
        return list(self._tools)


def test_lifespan_starts_and_stops_gateway_and_manager():
    gw, mgr = _FakeGateway(), _FakeManager()

    def factory():
        return Agent(llm=StubLLM([LLMResponse(text="ok")]), tools=ToolRegistry())

    app = create_app(factory, model_id="redcell", gateway=gw, mcp_manager=mgr)
    with TestClient(app) as client:  # entering the context runs the lifespan
        assert gw.started and mgr.entered
        assert client.get("/v1/models").status_code == 200
    assert gw.stopped and mgr.exited


def test_gateway_tool_is_callable_through_the_endpoint():
    async def ping(**kwargs):
        return "pong"

    ping.__name__ = "ping"
    gw_tool = Tool(
        ping,
        schema={"type": "object", "properties": {}, "required": []},
        name="ping",
        description="ping",
    )
    mgr = _FakeManager(tools=[gw_tool])
    scripted = [
        LLMResponse(text=None, tool_calls=[ToolCall(id="c1", name="ping", arguments={})]),
        LLMResponse(text="done"),
    ]

    def factory():
        reg = ToolRegistry()
        for t in mgr.tools():
            reg.register(t)
        return Agent(llm=StubLLM(list(scripted)), tools=reg)

    app = create_app(factory, model_id="redcell", mcp_manager=mgr)
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "redcell", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.json()["choices"][0]["message"]["content"] == "done"


def make_session_client(scripted, **app_kwargs):
    """A TestClient backed by a shared StubLLM and an enabled SessionStore.

    The single ``llm`` is reused across requests so ``llm.calls`` accumulates and
    tests can assert what history a later turn's LLM call actually saw.
    """
    llm = StubLLM(list(scripted))
    store = SessionStore()

    def factory():
        return Agent(llm=llm, tools=ToolRegistry(), system_prompt="SYS")

    client = TestClient(create_app(factory, model_id="redcell", session_store=store, **app_kwargs))
    return client, llm, store


def _contents(messages):
    return [m.get("content") for m in messages]


def test_session_remembers_history_across_requests():
    client, llm, store = make_session_client(
        [LLMResponse(text="noted"), LLMResponse(text="you are Travis")]
    )
    sid = "conv-1"
    r1 = client.post(
        "/v1/chat/completions",
        headers={"x-redcell-session": sid},
        json={"model": "redcell", "messages": [{"role": "user", "content": "My name is Travis."}]},
    )
    assert r1.status_code == 200
    assert r1.headers["x-redcell-session"] == sid  # echoed back

    r2 = client.post(
        "/v1/chat/completions",
        headers={"x-redcell-session": sid},
        json={"model": "redcell", "messages": [{"role": "user", "content": "What is my name?"}]},
    )
    assert r2.status_code == 200
    # The second turn's LLM call saw the first turn (user message + stored reply).
    second = _contents(llm.calls[1]["messages"])
    assert "My name is Travis." in second
    assert "noted" in second
    assert len(store) == 1


def test_distinct_sessions_are_isolated():
    client, llm, _ = make_session_client([LLMResponse(text="a-reply"), LLMResponse(text="b-reply")])
    client.post(
        "/v1/chat/completions",
        headers={"x-redcell-session": "A"},
        json={"model": "redcell", "messages": [{"role": "user", "content": "secret-A"}]},
    )
    client.post(
        "/v1/chat/completions",
        headers={"x-redcell-session": "B"},
        json={"model": "redcell", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert "secret-A" not in _contents(llm.calls[1]["messages"])


def test_no_session_header_is_stateless():
    client, llm, store = make_session_client([LLMResponse(text="r1"), LLMResponse(text="r2")])
    client.post(
        "/v1/chat/completions",
        json={"model": "redcell", "messages": [{"role": "user", "content": "remember X"}]},
    )
    r2 = client.post(
        "/v1/chat/completions",
        json={"model": "redcell", "messages": [{"role": "user", "content": "what?"}]},
    )
    assert "remember X" not in _contents(llm.calls[1]["messages"])
    assert len(store) == 0  # nothing stored without a session id
    assert "x-redcell-session" not in r2.headers


def test_session_id_from_body_field():
    client, llm, store = make_session_client(
        [LLMResponse(text="noted"), LLMResponse(text="recall")]
    )
    for content in ("fact one", "again"):
        client.post(
            "/v1/chat/completions",
            json={
                "model": "redcell",
                "session_id": "conv-b",
                "messages": [{"role": "user", "content": content}],
            },
        )
    assert "fact one" in _contents(llm.calls[1]["messages"])
    assert len(store) == 1


def test_session_streaming_persists_turn():
    client, llm, _ = make_session_client([LLMResponse(text="hello"), LLMResponse(text="again")])
    r1 = client.post(
        "/v1/chat/completions",
        headers={"x-redcell-session": "s"},
        json={
            "model": "redcell",
            "stream": True,
            "messages": [{"role": "user", "content": "first"}],
        },
    )
    assert r1.headers["x-redcell-session"] == "s"
    client.post(
        "/v1/chat/completions",
        headers={"x-redcell-session": "s"},
        json={"model": "redcell", "messages": [{"role": "user", "content": "second"}]},
    )
    # The streamed first turn was persisted, so the second turn's LLM sees it.
    assert "first" in _contents(llm.calls[1]["messages"])


def test_startup_failure_still_stops_gateway():
    # If the MCP manager fails to enter after the gateway started, the lifespan
    # must still stop the gateway (no leaked process).
    gw = _FakeGateway()

    class _BadManager:
        async def __aenter__(self):
            raise RuntimeError("connect failed")

        async def __aexit__(self, *exc):
            pass

    def factory():
        return Agent(llm=StubLLM([LLMResponse(text="ok")]), tools=ToolRegistry())

    app = create_app(factory, gateway=gw, mcp_manager=_BadManager())
    with pytest.raises(RuntimeError, match="connect failed"):
        with TestClient(app):
            pass
    assert gw.started and gw.stopped
