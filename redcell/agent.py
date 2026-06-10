"""The agent: an async tool-use loop over an LLM, tools, and memory."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from .guardrails import Guardrail, NullGuardrail
from .llm import LLMResponse
from .memory import InMemoryStore, Memory
from .observability import Hooks
from .permissions import NullPolicy, Policy
from .tools import ToolRegistry

# Tool results are echoed into the `tool_end` event for observability; cap the
# echoed length so a large result (a file dump, a page fetch) doesn't flood logs.
_MAX_RESULT_ECHO = 2000


def _new_run_id() -> str:
    """A short correlation id grouping every event emitted within one turn."""
    return uuid.uuid4().hex[:12]


class _Completer(Protocol):
    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> LLMResponse: ...


@dataclass
class ChatResult:
    """Outcome of a stateless completion: final text plus side channels."""

    text: str | None
    reasoning: str | None = None
    usage: dict = field(default_factory=dict)


class Agent:
    """Runs a conversation turn to completion, executing tools as requested.

    Args:
        llm: anything implementing ``complete(messages, tools) -> LLMResponse``.
        tools: registry of callable tools (defaults to empty).
        memory: conversation store (defaults to in-memory).
        system_prompt: optional system message prepended to every request.
        hooks: observability hooks fired around LLM and tool calls.
        max_iterations: hard cap on tool-call rounds per ``run``.
        guardrail: input/output moderation (defaults to a no-op, i.e. the
            unguarded "vulnerable target" behavior).
        enforce_system_prompt: when True, ``system_prompt`` always wins — any
            caller-supplied system message is dropped so a client cannot suppress
            it by sending its own ``system`` role. ``serve`` sets this whenever the
            safety prompt is on, closing the stateless-path bypass. Defaults to
            False, leaving library callers free to own the system message.
        max_concurrent_tools: cap on read-only tool calls run in parallel within
            a single turn (mutating calls always run serially regardless).
    """

    def __init__(
        self,
        llm: _Completer,
        tools: ToolRegistry | None = None,
        memory: Memory | None = None,
        system_prompt: str | None = None,
        hooks: Hooks | None = None,
        max_iterations: int = 10,
        guardrail: Guardrail | None = None,
        enforce_system_prompt: bool = False,
        max_concurrent_tools: int = 8,
        policy: Policy | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools or ToolRegistry()
        self.memory = memory or InMemoryStore()
        self.system_prompt = system_prompt
        self.hooks = hooks or Hooks()
        self.max_iterations = max_iterations
        self.guardrail = guardrail or NullGuardrail()
        self.enforce_system_prompt = enforce_system_prompt
        self.max_concurrent_tools = max(1, max_concurrent_tools)
        # Permission policy consulted before every tool dispatch (default: allow all).
        self.policy = policy or NullPolicy()

    async def _blocked_input(self, text: str, run_id: str) -> ChatResult | None:
        """Screen one user input; return a refusal result if the guardrail blocks."""
        if not text:
            return None
        verdict = await self.guardrail.check_input(text)
        if not verdict.allowed:
            self.hooks.emit(
                "guardrail_input_block",
                run_id=run_id,
                reason=verdict.reason,
                categories=verdict.categories,
            )
            return ChatResult(text=verdict.text)
        return None

    async def _screen_output(self, result: ChatResult, run_id: str) -> ChatResult:
        """Redact the final text through the guardrail before returning it."""
        if result.text is None:
            return result
        verdict = await self.guardrail.check_output(result.text)
        if verdict.text != result.text:
            self.hooks.emit(
                "guardrail_output_redact",
                run_id=run_id,
                reason=verdict.reason,
                categories=verdict.categories,
            )
            return ChatResult(text=verdict.text, reasoning=result.reasoning, usage=result.usage)
        return result

    @staticmethod
    def _latest_user_text(messages: list[dict]) -> str:
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content")
                return content if isinstance(content, str) else ""
        return ""

    def _messages(self) -> list[dict]:
        history = self.memory.load()
        if self.system_prompt:
            return [{"role": "system", "content": self.system_prompt}, *history]
        return history

    async def run(self, user_input: str, *, correlation_id: str | None = None) -> str:
        """Process one user input against the agent's memory, returning text."""
        run_id = correlation_id or _new_run_id()
        try:
            blocked = await self._blocked_input(user_input, run_id)
            if blocked is not None:
                return blocked.text or ""
            self.memory.append({"role": "user", "content": user_input})
            messages = self._messages()
            base = len(messages)
            result = await self._run_loop(messages, run_id)
            # Persist only the turns the loop appended (system prompt is not stored).
            for msg in messages[base:]:
                self.memory.append(msg)
            return (await self._screen_output(result, run_id)).text or ""
        finally:
            self.hooks.emit("run_end", run_id=run_id)

    async def run_session(
        self, memory: Memory, incoming: list[dict], *, correlation_id: str | None = None
    ) -> ChatResult:
        """Run one turn against server-side ``memory``, persisting the new turns.

        Unlike :meth:`run_messages` (where the caller owns history), the agent
        recalls prior turns from ``memory``, appends the ``incoming`` turn(s),
        runs the loop, and stores everything the loop produced — assistant text,
        tool-call turns and tool results — so the next turn sees real history.
        The system prompt is prepended for the LLM call but never persisted.

        ``correlation_id`` (e.g. the session id) tags every emitted event so a
        turn's events can be attributed even when many sessions interleave.
        """
        run_id = correlation_id or _new_run_id()
        try:
            blocked = await self._blocked_input(self._latest_user_text(incoming), run_id)
            if blocked is not None:
                return blocked
            # System messages are never conversation turns; drop any the client
            # sent so they can't be persisted and later suppress the system prompt.
            for msg in incoming:
                if msg.get("role") != "system":
                    memory.append(msg)
            history = memory.load()
            messages = self._with_system_prompt(history)
            base = len(messages)
            result = await self._run_loop(messages, run_id)
            for msg in messages[base:]:
                memory.append(msg)
            return await self._screen_output(result, run_id)
        finally:
            self.hooks.emit("run_end", run_id=run_id)

    async def run_messages(
        self, messages: list[dict], *, correlation_id: str | None = None
    ) -> ChatResult:
        """Run one completion from a caller-supplied message list (stateless).

        History is owned by the caller (e.g. an HTTP client), so the agent's own
        ``memory`` is left untouched. ``system_prompt`` is prepended only when
        the caller did not already include a system message.

        ``correlation_id`` tags every emitted event for this turn.
        """
        run_id = correlation_id or _new_run_id()
        try:
            blocked = await self._blocked_input(self._latest_user_text(messages), run_id)
            if blocked is not None:
                return blocked
            working = self._with_system_prompt(list(messages))
            result = await self._run_loop(working, run_id)
            return await self._screen_output(result, run_id)
        finally:
            self.hooks.emit("run_end", run_id=run_id)

    def _with_system_prompt(self, messages: list[dict]) -> list[dict]:
        """Prepend ``system_prompt`` to ``messages`` per the enforcement policy.

        With ``enforce_system_prompt`` the configured prompt always wins: any
        caller-supplied system message is dropped and ours is prepended, so a
        client cannot disable the safety policy by sending its own. Otherwise the
        prompt is added only when the caller did not already supply one.
        """
        if not self.system_prompt:
            return messages
        if self.enforce_system_prompt:
            kept = [m for m in messages if m.get("role") != "system"]
            return [{"role": "system", "content": self.system_prompt}, *kept]
        if not any(m.get("role") == "system" for m in messages):
            return [{"role": "system", "content": self.system_prompt}, *messages]
        return messages

    async def _run_loop(self, messages: list[dict], run_id: str) -> ChatResult:
        """Drive the tool-use loop over ``messages``, mutating it in place.

        Appends each assistant tool-call turn and the tool results to
        ``messages`` and repeats until the model returns a final text answer or
        ``max_iterations`` is hit. Returns the final text plus the reasoning and
        usage from the terminating response. ``run_id`` tags every emitted event.
        """
        last_usage: dict = {}
        model = getattr(self.llm, "model", "?")
        for _ in range(self.max_iterations):
            specs = self.tools.specs()
            self.hooks.emit("llm_start", run_id=run_id, model=model)
            response = await self.llm.complete(messages, tools=specs or None)
            self.hooks.emit("llm_end", run_id=run_id, model=model, usage=response.usage)
            last_usage = response.usage

            if not response.tool_calls:
                text = response.text or ""
                messages.append({"role": "assistant", "content": text})
                return ChatResult(text=text, reasoning=response.reasoning, usage=last_usage)

            # Record the assistant's tool-call turn, then execute concurrently.
            messages.append(
                {
                    "role": "assistant",
                    "content": response.text,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": _dumps(tc.arguments)},
                        }
                        for tc in response.tool_calls
                    ],
                }
            )
            results = await self._execute_tool_calls(response.tool_calls, run_id)
            for tc, result in zip(response.tool_calls, results, strict=True):
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": result}
                )

        message = f"Stopped: reached max_iterations ({self.max_iterations})."
        self.hooks.emit("max_iterations", run_id=run_id, limit=self.max_iterations)
        messages.append({"role": "assistant", "content": message})
        return ChatResult(text=message, reasoning=None, usage=last_usage)

    async def _execute_tool_calls(self, tool_calls: list, run_id: str) -> list[str]:
        """Run a turn's tool calls, parallelizing only consecutive safe runs.

        Consecutive concurrency-safe (read-only) calls execute together under a
        bounded pool; a mutating call runs alone and acts as a barrier. This
        keeps the parallelism win for read-heavy turns while preventing the
        flat-``gather`` footgun where a same-turn write and a dependent read (or
        two writes) race. Order of results matches the order of ``tool_calls``.
        """
        results: list[str] = [""] * len(tool_calls)
        sem = asyncio.Semaphore(self.max_concurrent_tools)

        async def run_one(idx: int) -> None:
            tc = tool_calls[idx]
            async with sem:
                results[idx] = await self._run_tool(tc.name, tc.arguments, tc.id, run_id)

        i, n = 0, len(tool_calls)
        while i < n:
            if self.tools.is_concurrency_safe(tool_calls[i].name, tool_calls[i].arguments):
                j = i
                while j < n and self.tools.is_concurrency_safe(
                    tool_calls[j].name, tool_calls[j].arguments
                ):
                    j += 1
                await asyncio.gather(*(run_one(k) for k in range(i, j)))  # parallel run
                i = j
            else:
                await run_one(i)  # mutating call: serial barrier
                i += 1
        return results

    async def _run_tool(self, name: str, arguments: dict, call_id: str, run_id: str) -> str:
        # Tool args are emitted RAW (not redacted): observing an exfiltration
        # attempt in a tool call is the point — the gateway is the choke point.
        self.hooks.emit("tool_start", run_id=run_id, name=name, args=arguments, id=call_id)
        started = time.perf_counter()

        # Permission gate: consult the policy before the tool runs. A blocked call
        # never reaches the tool; an "ask" resolved to allow still runs but is
        # recorded. Plain allows are silent (no event) to avoid noise.
        decision = self.policy.evaluate(name, arguments)
        if decision.behavior != "allow":
            self.hooks.emit(
                "permission",
                run_id=run_id,
                name=name,
                id=call_id,
                behavior=decision.behavior,
                allowed=decision.allowed,
                reason=decision.reason,
                rule=decision.rule,
            )
        if not decision.allowed:
            blocked = (
                f"<tool_use_error>blocked by permission policy "
                f"({decision.behavior}: {decision.rule or decision.reason})</tool_use_error>"
            )
            self.hooks.emit(
                "tool_end",
                run_id=run_id,
                name=name,
                id=call_id,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                is_error=True,
                result=blocked,
            )
            return blocked

        outcome = await self.tools.invoke(name, arguments)
        result = outcome.content
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        # Echo the (truncated) RAW result so injection success — e.g. a RAG canary
        # surfacing in a tool's output — is visible in the event stream itself.
        echo = result
        if len(result) > _MAX_RESULT_ECHO:
            echo = result[:_MAX_RESULT_ECHO] + "…[truncated]"
        self.hooks.emit(
            "tool_end",
            run_id=run_id,
            name=name,
            id=call_id,
            duration_ms=duration_ms,
            is_error=outcome.is_error,
            result=echo,
        )
        # Screen the result before it reaches the model: a fetched secret or PII
        # read off the filesystem is redacted here so the model can't relay it.
        # Observability above already captured the raw value for measurement.
        verdict = await self.guardrail.check_output(result)
        if verdict.text != result:
            self.hooks.emit(
                "guardrail_tool_redact",
                run_id=run_id,
                name=name,
                id=call_id,
                reason=verdict.reason,
                categories=verdict.categories,
            )
            return verdict.text
        return result


def _dumps(value: object) -> str:
    import json

    return json.dumps(value)
