"""The agent: an async tool-use loop over an LLM, tools, and memory."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol

from .llm import LLMResponse
from .memory import InMemoryStore, Memory
from .observability import Hooks
from .tools import ToolRegistry


class _Completer(Protocol):
    async def complete(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        ...


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
    """

    def __init__(
        self,
        llm: _Completer,
        tools: ToolRegistry | None = None,
        memory: Memory | None = None,
        system_prompt: str | None = None,
        hooks: Hooks | None = None,
        max_iterations: int = 10,
    ) -> None:
        self.llm = llm
        self.tools = tools or ToolRegistry()
        self.memory = memory or InMemoryStore()
        self.system_prompt = system_prompt
        self.hooks = hooks or Hooks()
        self.max_iterations = max_iterations

    def _messages(self) -> list[dict]:
        history = self.memory.load()
        if self.system_prompt:
            return [{"role": "system", "content": self.system_prompt}, *history]
        return history

    async def run(self, user_input: str) -> str:
        """Process one user input against the agent's memory, returning text."""
        self.memory.append({"role": "user", "content": user_input})
        messages = self._messages()
        base = len(messages)
        result = await self._run_loop(messages)
        # Persist only the turns the loop appended (system prompt is not stored).
        for msg in messages[base:]:
            self.memory.append(msg)
        return result.text or ""

    async def run_messages(self, messages: list[dict]) -> ChatResult:
        """Run one completion from a caller-supplied message list (stateless).

        History is owned by the caller (e.g. an HTTP client), so the agent's own
        ``memory`` is left untouched. ``system_prompt`` is prepended only when
        the caller did not already include a system message.
        """
        working = list(messages)
        if self.system_prompt and not any(m.get("role") == "system" for m in working):
            working.insert(0, {"role": "system", "content": self.system_prompt})
        return await self._run_loop(working)

    async def _run_loop(self, messages: list[dict]) -> ChatResult:
        """Drive the tool-use loop over ``messages``, mutating it in place.

        Appends each assistant tool-call turn and the tool results to
        ``messages`` and repeats until the model returns a final text answer or
        ``max_iterations`` is hit. Returns the final text plus the reasoning and
        usage from the terminating response.
        """
        last_usage: dict = {}
        for _ in range(self.max_iterations):
            specs = self.tools.specs()
            self.hooks.emit("llm_start", model=getattr(self.llm, "model", "?"))
            response = await self.llm.complete(messages, tools=specs or None)
            self.hooks.emit("llm_end", usage=response.usage)
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
            results = await asyncio.gather(
                *(self._run_tool(tc.name, tc.arguments, tc.id) for tc in response.tool_calls)
            )
            for tc, result in zip(response.tool_calls, results, strict=True):
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": result}
                )

        message = f"Stopped: reached max_iterations ({self.max_iterations})."
        messages.append({"role": "assistant", "content": message})
        return ChatResult(text=message, reasoning=None, usage=last_usage)

    async def _run_tool(self, name: str, arguments: dict, call_id: str) -> str:
        self.hooks.emit("tool_start", name=name, args=arguments, id=call_id)
        result = await self.tools.invoke(name, arguments)
        self.hooks.emit("tool_end", name=name, id=call_id)
        return result


def _dumps(value: object) -> str:
    import json

    return json.dumps(value)
