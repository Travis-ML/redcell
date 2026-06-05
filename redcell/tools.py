"""Tool definition, JSON-schema generation, and a registry/executor."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable

_PY_TO_JSON = {
    int: "integer",
    float: "number",
    str: "string",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _json_type(annotation: object) -> str:
    return _PY_TO_JSON.get(annotation, "string")


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
    ) -> None:
        self.fn = fn
        self.name = name or fn.__name__
        self.description = description if description is not None else (inspect.getdoc(fn) or "")
        self.is_async = inspect.iscoroutinefunction(fn)
        self._schema = schema or self._build_schema(fn)

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


def tool(fn: Callable[..., object]) -> Tool:
    """Decorator turning a function into a :class:`Tool`.

    The JSON schema is derived from the signature's type hints; the
    description is taken from the docstring.
    """
    return Tool(fn)


class ToolRegistry:
    """Holds tools and executes them by name, catching errors as strings."""

    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {t.name: t for t in (tools or [])}

    def register(self, t: Tool) -> None:
        self._tools[t.name] = t

    def specs(self) -> list[dict]:
        """Return tool specs for every registered tool."""
        return [t.spec() for t in self._tools.values()]

    async def invoke(self, name: str, arguments: dict) -> str:
        """Execute a tool by name; return its result as a string.

        Unknown tools and raised exceptions are returned as error strings so
        the model can read them and recover.
        """
        t = self._tools.get(name)
        if t is None:
            return f"Error: unknown tool '{name}'"
        try:
            result = await t.call(arguments)
        except Exception as exc:  # surfaced back to the model, not raised
            return f"Error: {exc}"
        return result if isinstance(result, str) else json.dumps(result)
