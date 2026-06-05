from redcell.agent import Agent
from redcell.llm import LLMResponse, ToolCall
from redcell.memory import InMemoryStore
from redcell.tools import ToolRegistry, tool
from tests.conftest import StubLLM


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


async def test_plain_text_turn_no_tools():
    llm = StubLLM([LLMResponse(text="hello there")])
    agent = Agent(llm=llm, tools=ToolRegistry(), memory=InMemoryStore())
    out = await agent.run("hi")
    assert out == "hello there"
    # user + assistant recorded
    assert len(agent.memory.load()) == 2


async def test_tool_call_then_final_answer():
    llm = StubLLM(
        [
            LLMResponse(
                text=None, tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 2, "b": 3})]
            ),
            LLMResponse(text="The answer is 5"),
        ]
    )
    agent = Agent(llm=llm, tools=ToolRegistry([add]), memory=InMemoryStore())
    out = await agent.run("what is 2+3?")
    assert out == "The answer is 5"
    # second LLM call must include the tool result message
    roles = [m["role"] for m in llm.calls[1]["messages"]]
    assert "tool" in roles


async def test_run_messages_returns_chat_result():
    llm = StubLLM([LLMResponse(text="hi back", reasoning="thinking", usage={"total_tokens": 7})])
    agent = Agent(llm=llm, tools=ToolRegistry(), memory=InMemoryStore())
    result = await agent.run_messages([{"role": "user", "content": "hi"}])
    assert result.text == "hi back"
    assert result.reasoning == "thinking"
    assert result.usage["total_tokens"] == 7


async def test_run_messages_drives_tool_loop():
    llm = StubLLM(
        [
            LLMResponse(
                text=None, tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 2, "b": 3})]
            ),
            LLMResponse(text="The answer is 5"),
        ]
    )
    agent = Agent(llm=llm, tools=ToolRegistry([add]), memory=InMemoryStore())
    result = await agent.run_messages([{"role": "user", "content": "what is 2+3?"}])
    assert result.text == "The answer is 5"
    # second LLM call must include the tool result message
    roles = [m["role"] for m in llm.calls[1]["messages"]]
    assert "tool" in roles


async def test_run_messages_is_stateless():
    # The server owns history; run_messages must not mutate the agent's memory.
    llm = StubLLM([LLMResponse(text="ok")])
    agent = Agent(llm=llm, tools=ToolRegistry(), memory=InMemoryStore())
    await agent.run_messages([{"role": "user", "content": "hi"}])
    assert agent.memory.load() == []


async def test_run_messages_prepends_system_prompt_only_when_absent():
    llm = StubLLM([LLMResponse(text="a"), LLMResponse(text="b")])
    agent = Agent(llm=llm, tools=ToolRegistry(), system_prompt="SYS")
    # No system message present -> system_prompt is prepended.
    await agent.run_messages([{"role": "user", "content": "hi"}])
    assert llm.calls[0]["messages"][0] == {"role": "system", "content": "SYS"}
    # Caller-provided system message present -> not duplicated.
    await agent.run_messages(
        [{"role": "system", "content": "OWN"}, {"role": "user", "content": "hi"}]
    )
    systems = [m for m in llm.calls[1]["messages"] if m["role"] == "system"]
    assert systems == [{"role": "system", "content": "OWN"}]


async def test_max_iterations_guard():
    # Always asks for a tool call -> would loop forever without the guard.
    looping = [
        LLMResponse(
            text=None, tool_calls=[ToolCall(id="c", name="add", arguments={"a": 1, "b": 1})]
        )
        for _ in range(20)
    ]
    llm = StubLLM(looping)
    agent = Agent(llm=llm, tools=ToolRegistry([add]), memory=InMemoryStore(), max_iterations=3)
    out = await agent.run("loop")
    assert "max" in out.lower()  # surfaced as a message, not an exception
    assert len(llm.calls) == 3
