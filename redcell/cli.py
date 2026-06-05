"""Typer CLI: interactive chat REPL and version."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import typer
import uvicorn

from . import __version__
from .agent import Agent
from .config import Settings
from .gateway import GatewaySupervisor
from .llm import LLM
from .mcp import MCPManager, streamable_http_session
from .observability import configure_logging, logging_hooks
from .searxng import make_web_search
from .server import create_app
from .tools import ToolRegistry, tool

app = typer.Typer(help="redcell command-line interface.")


@tool
def add(a: float, b: float) -> float:
    """Add two numbers and return the sum."""
    return a + b


@tool
def utc_now() -> str:
    """Return the current UTC time in ISO 8601 format."""
    return datetime.now(UTC).isoformat()


def default_tools(settings: Settings) -> ToolRegistry:
    """Tools available to the CLI/served agent.

    Includes the SearXNG-backed `web_search` (bound to the configured instance).
    MCP-backed tools will register here too once that integration lands.
    """
    return ToolRegistry([add, utc_now, make_web_search(settings.searxng_url)])


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command()
def serve(
    host: str = typer.Option(None, "--host", help="Override AGENT_SERVER_HOST."),
    port: int = typer.Option(None, "--port", help="Override AGENT_SERVER_PORT."),
) -> None:
    """Run the OpenAI-compatible HTTP server (launches AgentGateway too)."""
    settings = Settings()
    configure_logging(settings.log_level)

    manager = MCPManager(lambda: streamable_http_session(settings.gateway_url))

    def build_agent() -> Agent:
        tools = default_tools(settings)
        for t in manager.tools():  # gateway-provided MCP tools (empty if offline)
            tools.register(t)
        return Agent(
            llm=LLM(
                settings.model,
                settings.temperature,
                settings.max_tokens,
                api_base=settings.api_base,
                api_key=settings.api_key,
            ),
            tools=tools,
            system_prompt="You are a helpful assistant.",
            hooks=logging_hooks(),
            max_iterations=settings.max_iterations,
        )

    gateway = None
    if settings.gateway_autostart:
        gateway = GatewaySupervisor(
            command=[settings.gateway_bin, "-f", settings.gateway_config_path],
            host=settings.gateway_host,
            port=settings.gateway_port,
            ready_timeout=settings.gateway_ready_timeout,
        )

    api = create_app(
        build_agent,
        model_id=settings.model_id,
        api_key=settings.server_api_key,
        gateway=gateway,
        mcp_manager=manager,
    )
    bind_host = host or settings.server_host
    bind_port = port or settings.server_port
    typer.echo(f"Serving agent ({settings.model}) as model '{settings.model_id}'.")
    typer.echo(f"  local:  http://127.0.0.1:{bind_port}/v1")
    typer.echo(f"  docker: http://host.docker.internal:{bind_port}/v1  (use this in Open WebUI)")
    if gateway is not None:
        typer.echo(f"  gateway: launching '{settings.gateway_bin}' on :{settings.gateway_port}")
    uvicorn.run(api, host=bind_host, port=bind_port)


@app.command()
def chat(system_prompt: str = typer.Option("You are a helpful assistant.", "--system")) -> None:
    """Start an interactive chat session with a basic agent."""
    settings = Settings()
    configure_logging(settings.log_level)
    agent = Agent(
        llm=LLM(
            settings.model,
            settings.temperature,
            settings.max_tokens,
            api_base=settings.api_base,
            api_key=settings.api_key,
        ),
        tools=default_tools(settings),
        system_prompt=system_prompt,
        hooks=logging_hooks(),
        max_iterations=settings.max_iterations,
    )

    typer.echo(f"redcell chat ({settings.model}). Ctrl-C to exit.")

    async def _loop() -> None:
        while True:
            user = typer.prompt("you")
            reply = await agent.run(user)
            typer.echo(f"agent: {reply}")

    try:
        asyncio.run(_loop())
    except (KeyboardInterrupt, EOFError):
        typer.echo("\nbye")


if __name__ == "__main__":
    app()
