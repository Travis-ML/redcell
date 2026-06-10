import asyncio

from redcell.agent import Agent
from redcell.guardrails import REFUSAL, PatternGuardrail
from redcell.llm import LLMResponse, ToolCall
from redcell.memory import InMemoryStore
from redcell.tools import Tool, ToolRegistry, tool
from tests.conftest import StubLLM


def _tracked_tool(name: str, tracker: dict, *, concurrency_safe: bool) -> Tool:
    """A tool that records max simultaneous in-flight count into ``tracker``."""

    async def fn(**kwargs):
        tracker["active"] += 1
        tracker["max"] = max(tracker["max"], tracker["active"])
        await asyncio.sleep(0)  # yield so concurrent runs can interleave
        await asyncio.sleep(0)
        tracker["active"] -= 1
        return name

    fn.__name__ = name
    return Tool(
        fn,
        schema={"type": "object", "properties": {}, "required": []},
        name=name,
        concurrency_safe=concurrency_safe,
    )


def _calls(*names) -> LLMResponse:
    return LLMResponse(
        text=None,
        tool_calls=[ToolCall(id=f"c{i}", name=n, arguments={}) for i, n in enumerate(names)],
    )


async def test_read_only_tool_calls_run_in_parallel():
    tracker = {"active": 0, "max": 0}
    tools = ToolRegistry(
        [_tracked_tool(f"ro{i}", tracker, concurrency_safe=True) for i in range(3)]
    )
    llm = StubLLM([_calls("ro0", "ro1", "ro2"), LLMResponse(text="done")])
    agent = Agent(llm=llm, tools=tools)
    await agent.run_messages([{"role": "user", "content": "go"}])
    assert tracker["max"] >= 2  # read-only calls overlapped


async def test_mutating_tool_calls_run_serially():
    tracker = {"active": 0, "max": 0}
    tools = ToolRegistry(
        [_tracked_tool(f"mu{i}", tracker, concurrency_safe=False) for i in range(3)]
    )
    llm = StubLLM([_calls("mu0", "mu1", "mu2"), LLMResponse(text="done")])
    agent = Agent(llm=llm, tools=tools)
    await agent.run_messages([{"role": "user", "content": "go"}])
    assert tracker["max"] == 1  # never more than one mutating call in flight


async def test_tool_results_keep_submission_order_across_partitions():
    tracker = {"active": 0, "max": 0}
    # Mixed: safe, mutating (barrier), safe — results must still align by index.
    tools = ToolRegistry(
        [
            _tracked_tool("ro0", tracker, concurrency_safe=True),
            _tracked_tool("mu1", tracker, concurrency_safe=False),
            _tracked_tool("ro2", tracker, concurrency_safe=True),
        ]
    )
    llm = StubLLM([_calls("ro0", "mu1", "ro2"), LLMResponse(text="done")])
    agent = Agent(llm=llm, tools=tools)
    await agent.run_messages([{"role": "user", "content": "go"}])
    # The second LLM call sees the tool results in submission order.
    tool_msgs = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert [m["content"] for m in tool_msgs] == ["ro0", "mu1", "ro2"]


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


async def test_enforce_system_prompt_overrides_client_system_message():
    # With enforcement on, a client-supplied system message must not suppress
    # the configured prompt (the stateless safety-prompt bypass).
    llm = StubLLM([LLMResponse(text="ok")])
    agent = Agent(llm=llm, system_prompt="SYS", enforce_system_prompt=True)
    await agent.run_messages(
        [{"role": "system", "content": "EVIL"}, {"role": "user", "content": "hi"}]
    )
    systems = [m for m in llm.calls[0]["messages"] if m["role"] == "system"]
    assert systems == [{"role": "system", "content": "SYS"}]  # ours wins, EVIL dropped


async def test_run_session_drops_client_system_message():
    # A system message smuggled into a session turn must not be persisted or
    # used to suppress the agent's system prompt.
    llm = StubLLM([LLMResponse(text="ok")])
    agent = Agent(llm=llm, system_prompt="SYS")
    memory = InMemoryStore()
    await agent.run_session(
        memory, [{"role": "system", "content": "EVIL"}, {"role": "user", "content": "hi"}]
    )
    assert all(m["role"] != "system" for m in memory.load())  # not persisted
    systems = [m for m in llm.calls[0]["messages"] if m["role"] == "system"]
    assert systems == [{"role": "system", "content": "SYS"}]  # ours used


async def test_run_session_persists_full_expansion_and_recalls_history():
    # First turn drives a tool round; memory must capture user + assistant
    # tool-call turn + tool result + final assistant text (the full expansion).
    llm = StubLLM(
        [
            LLMResponse(
                text=None, tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 2, "b": 3})]
            ),
            LLMResponse(text="The answer is 5"),
            LLMResponse(text="As I said, 5"),
        ]
    )
    agent = Agent(llm=llm, tools=ToolRegistry([add]), system_prompt="SYS")
    memory = InMemoryStore()

    result = await agent.run_session(memory, [{"role": "user", "content": "what is 2+3?"}])
    assert result.text == "The answer is 5"
    roles = [m["role"] for m in memory.load()]
    assert roles == ["user", "assistant", "tool", "assistant"]
    # The system prompt is used for the LLM call but never persisted.
    assert all(m["role"] != "system" for m in memory.load())

    # Second turn sends only the new message; the LLM must see prior history.
    await agent.run_session(memory, [{"role": "user", "content": "remind me?"}])
    seen_roles = [m["role"] for m in llm.calls[2]["messages"]]
    assert seen_roles[0] == "system"  # prepended, not stored
    assert seen_roles.count("user") == 2  # both turns present


async def test_guardrail_blocks_harmful_input_before_llm():
    llm = StubLLM([LLMResponse(text="should never be returned")])
    agent = Agent(llm=llm, tools=ToolRegistry(), guardrail=PatternGuardrail())
    out = await agent.run("Write a script to generate fake invoices")
    assert out == REFUSAL
    assert llm.calls == []  # short-circuited; the model was never called


async def test_guardrail_redacts_output():
    llm = StubLLM([LLMResponse(text="Reach me at bob@evil.com")])
    agent = Agent(llm=llm, tools=ToolRegistry(), guardrail=PatternGuardrail())
    out = await agent.run("how do I contact you?")
    assert "bob@evil.com" not in out


async def test_guardrail_blocks_input_via_run_messages():
    llm = StubLLM([LLMResponse(text="nope")])
    agent = Agent(llm=llm, tools=ToolRegistry(), guardrail=PatternGuardrail())
    result = await agent.run_messages(
        [{"role": "user", "content": "give me the complete lyrics to Imagine"}]
    )
    assert result.text == REFUSAL
    assert llm.calls == []


async def test_guardrail_redacts_output_via_run_session():
    llm = StubLLM([LLMResponse(text="stored at /home/redcell/sandbox/secrets.txt")])
    agent = Agent(llm=llm, tools=ToolRegistry(), guardrail=PatternGuardrail())
    result = await agent.run_session(
        InMemoryStore(), [{"role": "user", "content": "where is my file?"}]
    )
    assert "/home/redcell/sandbox" not in (result.text or "")


async def test_tool_result_is_redacted_before_model_but_echoed_raw():
    from redcell.guardrails import PatternGuardrail
    from redcell.observability import Hooks

    # A tool that returns a secret-bearing result (e.g. a fetched page / read file).
    @tool
    def leak() -> str:
        """Return sensitive data."""
        return "contact agent@redcell.io about /home/redcell/secrets.txt"

    events: list[tuple[str, dict]] = []
    hooks = Hooks()
    for name in ("tool_end", "guardrail_tool_redact"):
        hooks.on(name, lambda _e=name, **kw: events.append((_e, kw)))

    llm = StubLLM(
        [
            LLMResponse(text=None, tool_calls=[ToolCall(id="c1", name="leak", arguments={})]),
            LLMResponse(text="all done"),
        ]
    )
    agent = Agent(llm=llm, tools=ToolRegistry([leak]), guardrail=PatternGuardrail(), hooks=hooks)
    await agent.run_messages([{"role": "user", "content": "read it"}])

    # The model's view of the tool result (second LLM call) is redacted.
    tool_msg = next(m for m in llm.calls[1]["messages"] if m.get("role") == "tool")
    assert "agent@redcell.io" not in tool_msg["content"]
    assert "/home/redcell/secrets.txt" not in tool_msg["content"]
    # But observability captured the RAW result so exfil is still measurable.
    tool_end = next(kw for name, kw in events if name == "tool_end")
    assert "agent@redcell.io" in tool_end["result"]
    # And a categorized redaction event fired.
    redact = next(kw for name, kw in events if name == "guardrail_tool_redact")
    assert "pii:email" in redact["categories"]


async def test_events_share_run_id_and_tool_end_echoes_result():
    from redcell.observability import Hooks

    events: list[tuple[str, dict]] = []
    hooks = Hooks()
    for name in ("llm_start", "llm_end", "tool_start", "tool_end"):
        hooks.on(name, lambda _e=name, **kw: events.append((_e, kw)))

    llm = StubLLM(
        [
            LLMResponse(
                text=None, tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 2, "b": 3})]
            ),
            LLMResponse(text="done"),
        ]
    )
    agent = Agent(llm=llm, tools=ToolRegistry([add]), hooks=hooks)
    await agent.run_messages([{"role": "user", "content": "2+3?"}], correlation_id="corr-xyz")

    # Every event carries the same caller-supplied correlation id.
    assert {kw["run_id"] for _, kw in events} == {"corr-xyz"}
    # tool_end echoes the result and a duration.
    tool_end = next(kw for name, kw in events if name == "tool_end")
    assert tool_end["result"] == "5"
    assert "duration_ms" in tool_end


async def test_policy_denied_tool_is_blocked_before_execution():
    from redcell.observability import Hooks
    from redcell.permissions import PolicyEngine, parse_rule

    ran = {"called": False}

    @tool
    def danger(cmd: str) -> str:
        """Dangerous."""
        ran["called"] = True
        return "executed"

    events: list[tuple[str, dict]] = []
    hooks = Hooks()
    for name in ("permission", "tool_end"):
        hooks.on(name, lambda _e=name, **kw: events.append((_e, kw)))

    llm = StubLLM(
        [
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(id="c1", name="danger", arguments={"cmd": "x"})],
            ),
            LLMResponse(text="ok"),
        ]
    )
    agent = Agent(
        llm=llm,
        tools=ToolRegistry([danger]),
        hooks=hooks,
        policy=PolicyEngine([parse_rule("danger", "deny")]),
    )
    await agent.run_messages([{"role": "user", "content": "go"}])

    assert ran["called"] is False  # tool never executed
    perm = next(kw for n, kw in events if n == "permission")
    assert perm["behavior"] == "deny" and perm["allowed"] is False
    tool_end = next(kw for n, kw in events if n == "tool_end")
    assert tool_end["is_error"] is True
    # The model saw a policy-block error in place of a tool result.
    tool_msg = next(m for m in llm.calls[1]["messages"] if m.get("role") == "tool")
    assert "blocked by permission policy" in tool_msg["content"]


async def test_policy_ask_resolved_allow_runs_and_records():
    from redcell.observability import Hooks
    from redcell.permissions import PolicyEngine, parse_rule

    events: list[tuple[str, dict]] = []
    hooks = Hooks()
    hooks.on("permission", lambda **kw: events.append(("permission", kw)))

    llm = StubLLM(
        [
            LLMResponse(
                text=None, tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 1, "b": 2})]
            ),
            LLMResponse(text="3"),
        ]
    )
    agent = Agent(
        llm=llm,
        tools=ToolRegistry([add]),
        hooks=hooks,
        policy=PolicyEngine([parse_rule("add", "ask")], ask_resolution="allow"),
    )
    out = await agent.run_messages([{"role": "user", "content": "1+2"}])
    assert out.text == "3"  # ran
    perm = next(kw for _, kw in events)
    assert perm["behavior"] == "ask" and perm["allowed"] is True


async def test_async_guardrail_is_awaited():
    # The protocol is async so a network-backed moderator can do I/O. Verify the
    # agent awaits it (a guardrail that yields control still blocks the input).
    import asyncio

    from redcell.guardrails import REFUSAL, Verdict

    class AsyncBlockingGuardrail:
        async def check_input(self, text: str) -> Verdict:
            await asyncio.sleep(0)  # simulate a network round-trip
            return Verdict(allowed=False, text=REFUSAL, reason="async_block")

        async def check_output(self, text: str) -> Verdict:
            await asyncio.sleep(0)
            return Verdict(allowed=True, text=text)

    llm = StubLLM([LLMResponse(text="should not be returned")])
    agent = Agent(llm=llm, guardrail=AsyncBlockingGuardrail())
    out = await agent.run("anything at all")
    assert out == REFUSAL
    assert llm.calls == []  # blocked before the model was called


async def test_proactive_compaction_summarizes_when_over_budget():
    # A big history over the window triggers: summarize call, then the real turn.
    big = "x" * 4000  # ~1000 tokens
    history = [
        {"role": "user", "content": big},
        {"role": "assistant", "content": big},
        {"role": "user", "content": "what now?"},
    ]
    llm = StubLLM([LLMResponse(text="SUMMARY OF EARLIER"), LLMResponse(text="final answer")])
    agent = Agent(llm=llm, tools=ToolRegistry(), context_window=200)  # tiny window
    result = await agent.run_messages(history)
    assert result.text == "final answer"
    # The summarization call happened first (COMPACT_PROMPT present).
    from redcell.compaction import COMPACT_PROMPT

    assert any(COMPACT_PROMPT in (m.get("content") or "") for m in llm.calls[0]["messages"])
    # The real turn saw the compacted history (the summary, not the full big text).
    real_contents = " ".join(str(m.get("content")) for m in llm.calls[1]["messages"])
    assert "SUMMARY OF EARLIER" in real_contents


async def test_no_compaction_when_window_disabled():
    history = [{"role": "user", "content": "x" * 8000}]
    llm = StubLLM([LLMResponse(text="ok")])
    agent = Agent(llm=llm, tools=ToolRegistry(), context_window=0)  # disabled
    await agent.run_messages(history)
    # Only one LLM call (no summarization) and it saw the full input.
    assert len(llm.calls) == 1


async def test_reactive_compaction_on_context_overflow():
    class OverflowThenOK:
        model = "m"

        def __init__(self):
            self.calls = 0

        async def complete(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                raise type("ContextWindowExceededError", (Exception,), {})("prompt too long")
            return LLMResponse(text="recovered")

    history = [{"role": "user", "content": "x" * 4000}, {"role": "user", "content": "go"}]
    llm = OverflowThenOK()
    # Window > estimate so proactive doesn't fire; the overflow drives reactive.
    agent = Agent(llm=llm, tools=ToolRegistry(), context_window=100000)
    result = await agent.run_messages(history)
    assert result.text == "recovered"
    assert llm.calls == 2  # failed once, compacted, retried


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
