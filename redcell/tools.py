"""Tool definition, JSON-schema generation, and a registry/executor."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass

_PY_TO_JSON = {
    int: "integer",
    float: "number",
    str: "string",
    bool: "boolean",
    list: "array",
    dict: "object",
}

# Cap on tool-result content handed back to the model. Large results (file
# dumps, page fetches, multi-MB tracebacks) are head/tail truncated so they
# can't blow the context window or the token budget.
_MAX_TOOL_RESULT_CHARS = 16000
_TOOL_ERROR_OPEN = "<tool_use_error>"
_TOOL_ERROR_CLOSE = "</tool_use_error>"

# A classification flag is either a fixed bool or a predicate over the call's
# arguments (the same tool can be read-only or mutating depending on its input).
Classifier = bool | Callable[[dict], bool]


def _json_type(annotation: object) -> str:
    return _PY_TO_JSON.get(annotation, "string")


def _eval_flag(flag: Classifier, arguments: dict, *, fail: bool) -> bool:
    """Resolve a bool-or-callable classifier; return ``fail`` if it raises."""
    if callable(flag):
        try:
            return bool(flag(arguments))
        except Exception:
            return fail  # fail-closed: a classifier that errors is treated conservatively
    return bool(flag)


def _truncate(text: str) -> str:
    """Head/tail truncate over-long tool output, marking what was dropped."""
    if len(text) <= _MAX_TOOL_RESULT_CHARS:
        return text
    half = _MAX_TOOL_RESULT_CHARS // 2
    omitted = len(text) - 2 * half
    return f"{text[:half]}\n…[{omitted} chars truncated]…\n{text[-half:]}"


@dataclass
class ToolResult:
    """Outcome of a tool invocation: model-facing content plus an error flag.

    ``is_error`` lets the agent and observability distinguish a failure from a
    normal result even though the OpenAI tool-message wire format has no such
    field (the content is also wrapped in ``<tool_use_error>`` so the model
    reliably recognizes the failure).
    """

    content: str
    is_error: bool = False


class Tool:
    """A callable exposed to the model, with a generated JSON schema.

    For local functions, use the :func:`tool` decorator. Remote tools (e.g. MCP)
    that bring their own name, description, and schema construct this directly
    with the ``schema``/``name``/``description`` overrides.
    """

    def __init__(
        self,
        fn: Callable[..., object],
        schema: dict | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
        read_only: Classifier = False,
        concurrency_safe: Classifier = False,
        destructive: Classifier = False,
        source: str = "builtin",
    ) -> None:
        self.fn = fn
        self.name = name or fn.__name__
        self.description = description if description is not None else (inspect.getdoc(fn) or "")
        self.is_async = inspect.iscoroutinefunction(fn)
        self._schema = schema or self._build_schema(fn)
        # Classification flags — fixed bool or predicate over the call args.
        # Defaults are conservative (fail-closed): a tool that doesn't declare
        # itself read-only/concurrency-safe is assumed to mutate and to be
        # unsafe to run in parallel. ``source`` distinguishes builtin vs mcp.
        self.read_only = read_only
        self.concurrency_safe = concurrency_safe
        self.destructive = destructive
        self.source = source

    def is_read_only(self, arguments: dict) -> bool:
        """Whether this call only reads (fail-closed to mutating on error)."""
        return _eval_flag(self.read_only, arguments, fail=False)

    def is_concurrency_safe(self, arguments: dict) -> bool:
        """Whether this call is safe to run in parallel (fail-closed to serial)."""
        return _eval_flag(self.concurrency_safe, arguments, fail=False)

    def is_destructive(self, arguments: dict) -> bool:
        """Whether this call is destructive (fail-closed to destructive on error)."""
        return _eval_flag(self.destructive, arguments, fail=True)

    @staticmethod
    def _build_schema(fn: Callable[..., object]) -> dict:
        sig = inspect.signature(fn)
        properties: dict[str, dict] = {}
        required: list[str] = []
        for name, param in sig.parameters.items():
            properties[name] = {"type": _json_type(param.annotation)}
            if param.default is inspect.Parameter.empty:
                required.append(name)
        return {"type": "object", "properties": properties, "required": required}

    def spec(self) -> dict:
        """Return the OpenAI-format tool spec for this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._schema,
            },
        }

    async def call(self, arguments: dict) -> object:
        """Invoke the underlying function with keyword ``arguments``."""
        if self.is_async:
            return await self.fn(**arguments)
        return await asyncio.to_thread(self.fn, **arguments)


def tool(
    fn: Callable[..., object] | None = None,
    *,
    read_only: Classifier = False,
    concurrency_safe: Classifier = False,
    destructive: Classifier = False,
) -> Tool | Callable[[Callable[..., object]], Tool]:
    """Decorator turning a function into a :class:`Tool`.

    The JSON schema is derived from the signature's type hints; the description
    is taken from the docstring. Usable bare (``@tool``) or with classification
    (``@tool(read_only=True, concurrency_safe=True)``).
    """

    def wrap(f: Callable[..., object]) -> Tool:
        return Tool(
            f,
            read_only=read_only,
            concurrency_safe=concurrency_safe,
            destructive=destructive,
        )

    return wrap(fn) if fn is not None else wrap


class ToolRegistry:
    """Holds tools and executes them by name, catching errors as strings."""

    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {t.name: t for t in (tools or [])}

    def register(self, t: Tool, *, overwrite: bool = True) -> bool:
        """Add a tool. With ``overwrite=False`` a name collision is refused.

        Returns True if the tool was added, False if it was skipped because a
        tool of that name already exists (used so builtins shadow same-named MCP
        tools rather than being silently replaced).
        """
        if not overwrite and t.name in self._tools:
            return False
        self._tools[t.name] = t
        return True

    def names(self) -> set[str]:
        """The names of all registered tools."""
        return set(self._tools)

    def specs(self) -> list[dict]:
        """Return tool specs for every registered tool."""
        return [t.spec() for t in self._tools.values()]

    def is_concurrency_safe(self, name: str, arguments: dict) -> bool:
        """Whether a call to ``name`` may run in parallel (unknown tool = no)."""
        t = self._tools.get(name)
        return bool(t and t.is_concurrency_safe(arguments))

    async def invoke(self, name: str, arguments: dict) -> ToolResult:
        """Execute a tool by name; return a :class:`ToolResult`.

        Unknown tools and raised exceptions become ``is_error`` results whose
        content is wrapped in ``<tool_use_error>`` (so the model reliably
        recognizes a failure) and head/tail truncated. Successful results are
        stringified and truncated to bound the context window.
        """
        t = self._tools.get(name)
        if t is None:
            return _error_result(f"unknown tool '{name}'")
        try:
            result = await t.call(arguments)
        except Exception as exc:  # surfaced back to the model, not raised
            return _error_result(f"{type(exc).__name__}: {exc}")
        content = result if isinstance(result, str) else json.dumps(result)
        return ToolResult(content=_truncate(content), is_error=False)


def _error_result(message: str) -> ToolResult:
    """A truncated, ``<tool_use_error>``-wrapped error :class:`ToolResult`."""
    body = _truncate(message)
    return ToolResult(content=f"{_TOOL_ERROR_OPEN}{body}{_TOOL_ERROR_CLOSE}", is_error=True)
