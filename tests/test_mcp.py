# tests/test_mcp.py
"""MCPManager wraps remote MCP tools as local Tools. Offline via in-memory transport."""

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from redcell.mcp import MCPManager


def _server() -> FastMCP:
    s = FastMCP("test")

    @s.tool()
    def echo(text: str) -> str:
        """Echo the text back."""
        return text

    @s.tool()
    def boom() -> str:
        """Always fails."""
        raise ValueError("kaboom")

    return s


def _factory(server):
    # Each call returns a fresh in-memory client/server session context manager.
    return lambda: create_connected_server_and_client_session(server)


async def test_manager_wraps_remote_tools():
    server = _server()
    async with MCPManager(_factory(server)) as mgr:
        tools = {t.name: t for t in mgr.tools()}
        assert {"echo", "boom"} <= set(tools)
        echo = tools["echo"]
        assert echo.description == "Echo the text back."
        assert echo.spec()["function"]["parameters"]["properties"]["text"]["type"] == "string"
        assert await echo.call({"text": "hi"}) == "hi"
        # MCP tools are tagged and classified conservatively (fail-closed).
        assert echo.source == "mcp"
        assert echo.is_read_only({}) is False
        assert echo.is_concurrency_safe({}) is False


async def test_manager_tool_error_returns_string():
    server = _server()
    async with MCPManager(_factory(server)) as mgr:
        boom = {t.name: t for t in mgr.tools()}["boom"]
        out = await boom.call({})
        assert out.startswith("Error:")


async def test_manager_degrades_when_connect_fails():
    def bad():
        raise RuntimeError("no endpoint")

    async with MCPManager(bad) as mgr:
        assert mgr.tools() == []


async def test_wrapped_tool_opens_a_session_per_call():
    # Tools are discovered once, but each call opens its own short-lived session,
    # so a tool stays usable after the manager's context has exited.
    server = _server()
    async with MCPManager(_factory(server)) as mgr:
        echo = {t.name: t for t in mgr.tools()}["echo"]
    assert await echo.call({"text": "hi"}) == "hi"
