"""The agent: an async tool-use loop over an LLM, tools, and memory."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from .compaction import (
    COMPACT_PROMPT,
    build_compacted,
    estimate_tokens,
    is_context_overflow,
    microcompact,
    safe_split_index,
)
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
        policy: permission policy consulted before each tool dispatch (default:
            allow all).
        context_window: model context window in tokens; 0 disables compaction.
            When set, the loop keeps history under ``compact_ratio`` of the window
            by clearing old tool bodies and summarizing old turns (a recent tail is
            kept verbatim), and recovers from a real overflow by compacting + retry.
        compact_ratio: fraction of the window at which compaction kicks in.
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
        context_window: int = 0,
        compact_ratio: float = 0.8,
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
        # Context compaction: 0 disables. Compact when the estimate crosses
        # ``compact_ratio`` of the window; keep ~30% as a verbatim recent tail.
        self.context_window = context_window
        self._compact_threshold = int(context_window * compact_ratio)
        self._keep_recent_tokens = max(256, int(context_window * 0.3))

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
            result, new_turns = await self._run_loop(messages, run_id)
            # Persist only the turns the loop appended (system prompt is not
            # stored, and any compaction summary is ephemeral to the LLM view).
            for msg in new_turns:
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
            result, new_turns = await self._run_loop(messages, run_id)
            for msg in new_turns:
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
            result, _new_turns = await self._run_loop(working, run_id)
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

    async def _run_loop(self, messages: list[dict], run_id: str) -> tuple[ChatResult, list[dict]]:
        """Drive the tool-use loop over ``messages`` until a final answer.

        Returns ``(result, new_turns)`` — ``new_turns`` are the assistant/tool
        turns the loop appended (for the caller to persist). They are tracked
        separately from ``messages`` so context compaction can freely replace the
        LLM-facing list without disturbing what gets stored. ``run_id`` tags events.
        """
        last_usage: dict = {}
        model = getattr(self.llm, "model", "?")
        new_turns: list[dict] = []
        reactive_compacted = False

        def record(turn: dict) -> None:
            messages.append(turn)
            new_turns.append(turn)

        for _ in range(self.max_iterations):
            messages = await self._maybe_compact(messages, run_id)
            specs = self.tools.specs()
            self.hooks.emit("llm_start", run_id=run_id, model=model)
            try:
                response = await self.llm.complete(messages, tools=specs or None)
            except Exception as exc:
                # Reactive compaction: estimation can be wrong across backends, so
                # on a real context-overflow, compact hard and retry once.
                if self.context_window > 0 and not reactive_compacted and is_context_overflow(exc):
                    reactive_compacted = True
                    messages = await self._maybe_compact(messages, run_id, force=True)
                    response = await self.llm.complete(messages, tools=specs or None)
                else:
                    raise
            self.hooks.emit("llm_end", run_id=run_id, model=model, usage=response.usage)
            last_usage = response.usage

            if not response.tool_calls:
                text = response.text or ""
                record({"role": "assistant", "content": text})
                final = ChatResult(text=text, reasoning=response.reasoning, usage=last_usage)
                return final, new_turns

            # Record the assistant's tool-call turn, then execute concurrently.
            record(
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
                record({"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": result})

        message = f"Stopped: reached max_iterations ({self.max_iterations})."
        self.hooks.emit("max_iterations", run_id=run_id, limit=self.max_iterations)
        record({"role": "assistant", "content": message})
        return ChatResult(text=message, reasoning=None, usage=last_usage), new_turns

    async def _maybe_compact(
        self, messages: list[dict], run_id: str, *, force: bool = False
    ) -> list[dict]:
        """Compact ``messages`` if over budget: microcompact first, then summarize.

        Returns a (possibly new) list under the token threshold. With ``force`` it
        compacts regardless of the estimate (the reactive-overflow path).
        """
        if self.context_window <= 0:
            return messages
        if not force and estimate_tokens(messages) <= self._compact_threshold:
            return messages

        before = estimate_tokens(messages)
        # Cheap tier: clear old tool-result bodies.
        compacted = microcompact(messages)
        if not force and estimate_tokens(compacted) <= self._compact_threshold:
            self.hooks.emit(
                "compaction",
                run_id=run_id,
                kind="microcompact",
                before=before,
                after=estimate_tokens(compacted),
            )
            return compacted

        # Full tier: summarize the prefix, keep an invariant-safe recent tail.
        idx = safe_split_index(compacted, self._keep_recent_tokens)
        floor = 1 if compacted and compacted[0].get("role") == "system" else 0
        if idx <= floor:
            return compacted  # nothing safe to summarize; best-effort microcompact
        try:
            summary = await self._summarize(compacted[:idx])
        except Exception:  # summarization failed — drop the prefix rather than crash
            summary = "(earlier turns dropped: summary unavailable)"
        result = build_compacted(compacted, summary, idx)
        self.hooks.emit(
            "compaction",
            run_id=run_id,
            kind="summarize",
            before=before,
            after=estimate_tokens(result),
        )
        return result

    async def _summarize(self, prefix: list[dict]) -> str:
        """Summarize ``prefix`` into a compact digest via a tool-free LLM call."""
        resp = await self.llm.complete(
            [*prefix, {"role": "user", "content": COMPACT_PROMPT}], tools=None
        )
        return resp.text or "(summary unavailable)"

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
