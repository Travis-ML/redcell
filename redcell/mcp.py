"""Connect to a single MCP endpoint (AgentGateway) and expose its tools.

The agent talks to one upstream MCP server — AgentGateway — which itself
aggregates the real backends (Playwright, Filesystem, Fetch). This module turns
each remote MCP tool into a local :class:`~redcell.tools.Tool`.

Resilient by design: if the endpoint can't be reached, ``tools()`` is empty and
the agent simply runs with its builtin tools.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from mcp import ClientSession

from .tools import Tool

logger = logging.getLogger("redcell.mcp")

SessionFactory = Callable[[], AbstractAsyncContextManager[ClientSession]]


def _flatten(result: object) -> str:
    """Render an MCP ``CallToolResult`` as a single model-readable string."""
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else f"[{getattr(block, 'type', 'content')}]")
    out = "\n".join(parts)
    if getattr(result, "isError", False):
        return f"Error: {out}"
    return out


@asynccontextmanager
async def streamable_http_session(url: str) -> AsyncIterator[ClientSession]:
    """Open and initialize a ``ClientSession`` over Streamable HTTP."""
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


class MCPManager:
    """Expose an MCP endpoint's tools as local Tools, one session per call.

    Tools are discovered once on ``__aenter__``; each tool invocation then opens
    its OWN short-lived MCP session (connect, call, close). This avoids sharing a
    long-lived streamable-HTTP session across the web server's per-request tasks,
    which the protocol invalidates (the server ties the session id to the SSE
    stream of the task that opened it). Per-call sessions are robust and, for a
    test harness, cheap enough.

    Resilient by design: if discovery fails, ``tools()`` is empty and the agent
    runs with its builtin tools; if a single call fails, it returns an error
    string the model can recover from.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._tools: list[Tool] = []

    async def __aenter__(self) -> MCPManager:
        try:
            async with self._session_factory() as session:
                listed = await session.list_tools()
                self._tools = [self._wrap(t) for t in listed.tools]
            logger.info("MCP discovered %d tools", len(self._tools))
        except Exception as exc:  # degrade to builtins-only
            logger.warning("MCP discovery failed (%s); continuing without MCP tools", exc)
            self._tools = []
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        # Sessions are per-call and self-closing; nothing to tear down here.
        return None

    def tools(self) -> list[Tool]:
        """The local Tools wrapping each remote MCP tool (empty if discovery failed)."""
        return list(self._tools)

    def _wrap(self, remote: object) -> Tool:
        name = remote.name  # type: ignore[attr-defined]

        async def call(**kwargs: object) -> str:
            try:
                async with self._session_factory() as session:
                    result = await session.call_tool(name, kwargs)
                    return _flatten(result)
            except Exception as exc:  # surfaced to the model, not raised
                return f"Error: {exc}"

        call.__name__ = name
        return Tool(
            call,
            schema=remote.inputSchema,  # type: ignore[attr-defined]
            name=name,
            description=remote.description or "",  # type: ignore[attr-defined]
        )
