"""Typer CLI: interactive chat REPL and version."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import typer
import uvicorn

from . import __version__
from .agent import Agent
from .config import Settings
from .gateway import GatewaySupervisor
from .guardrails import make_guardrail
from .llm import LLM
from .mcp import MCPManager, streamable_http_session
from .observability import configure_logging, logging_hooks
from .prompts import build_system_prompt
from .qdrant import QdrantSupervisor
from .rag.corpus import default_corpus_path, load_corpus
from .rag.documents import ingest_documents
from .rag.seed import seed as rag_seed_corpus
from .searxng import make_web_search
from .server import create_app
from .sessions import SessionStore
from .tools import Tool, ToolRegistry, tool


def _denied_mcp_tools(settings: Settings) -> set[str]:
    """Parse AGENT_MCP_TOOL_DENYLIST into a set of lowercased match terms."""
    return {name.strip().lower() for name in settings.mcp_tool_denylist.split(",") if name.strip()}


def _safety_rules(settings: Settings) -> list[str] | None:
    """Parse AGENT_SAFETY_RULES; None (empty) means include all rules."""
    names = [name.strip() for name in settings.safety_rules.split(",") if name.strip()]
    return names or None


def apply_denylist(tools: list[Tool], denied: set[str]) -> tuple[list[Tool], list[str]]:
    """Split ``tools`` into (kept, dropped_names) using substring matching.

    A denylist entry matches any tool whose (lowercased) name *contains* it, so a
    gateway *target* name like ``shell`` drops every tool that target exposes even
    when the gateway namespaces them (e.g. ``shell_run_command``), and an exact
    tool name like ``run_command`` also works. Exact-only matching silently failed
    on namespaced names — a denied capability that stayed enabled.
    """
    kept: list[Tool] = []
    dropped: list[str] = []
    for t in tools:
        if any(term in t.name.lower() for term in denied):
            dropped.append(t.name)
        else:
            kept.append(t)
    return kept, dropped


def unmatched_denylist(tool_names: list[str], denied: set[str]) -> list[str]:
    """Denylist terms that matched no tool — likely a typo or wrong name."""
    lowered = [n.lower() for n in tool_names]
    return sorted(term for term in denied if not any(term in n for n in lowered))


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
    configure_logging(
        settings.log_level,
        json_logs=settings.log_json,
        log_file=settings.log_file,
        quiet_mcp_transport=settings.log_quiet_mcp_transport,
    )

    manager = MCPManager(lambda: streamable_http_session(settings.gateway_url))
    denied = _denied_mcp_tools(settings)
    denylist_reported: list[bool] = []  # one-shot guard for the startup summary

    def build_agent() -> Agent:
        tools = default_tools(settings)
        gateway_tools = manager.tools()  # gateway-provided MCP tools (empty if offline)
        kept, dropped = apply_denylist(gateway_tools, denied)
        for t in kept:
            tools.register(t)
        if denied and not denylist_reported:
            # Log once, after discovery, so a denylist that matched nothing is
            # loud rather than a silently-enabled "denied" capability.
            denylist_reported.append(True)
            log = logging.getLogger("redcell.cli")
            log.info("denylist dropped %d MCP tool(s): %s", len(dropped), ", ".join(dropped) or "—")
            stale = unmatched_denylist([t.name for t in gateway_tools], denied)
            if stale:
                log.warning(
                    "denylist term(s) matched no gateway tool: %s "
                    "(tools seen: %s) — capability NOT removed; check names in serve logs",
                    ", ".join(stale),
                    ", ".join(t.name for t in gateway_tools) or "none",
                )
        return Agent(
            llm=LLM(
                settings.model,
                settings.temperature,
                settings.max_tokens,
                api_base=settings.api_base,
                api_key=settings.api_key,
            ),
            tools=tools,
            system_prompt=build_system_prompt(
                safety=settings.safety_prompt, rules=_safety_rules(settings)
            ),
            hooks=logging_hooks(),
            max_iterations=settings.max_iterations,
            guardrail=make_guardrail(settings.guardrails),
            # When safety is on, the policy must not be suppressible by a client
            # sending its own system message (the stateless-path bypass).
            enforce_system_prompt=settings.safety_prompt,
        )

    gateway = None
    if settings.gateway_autostart:
        gateway = GatewaySupervisor(
            command=[settings.gateway_bin, "-f", settings.gateway_config_path],
            host=settings.gateway_host,
            port=settings.gateway_port,
            ready_timeout=settings.gateway_ready_timeout,
        )

    qdrant = None
    if settings.qdrant_autostart:
        qdrant = QdrantSupervisor(
            compose_file=settings.qdrant_compose_file,
            service=settings.qdrant_service,
            host=settings.qdrant_host,
            port=settings.qdrant_port,
            ready_timeout=settings.qdrant_ready_timeout,
            stop_on_exit=settings.qdrant_stop_on_exit,
        )

    session_store = SessionStore(
        max_sessions=settings.session_max,
        ttl_seconds=settings.session_ttl_seconds,
    )

    post_startup = None
    if settings.docs_autoload and settings.docs_dir:

        async def post_startup() -> None:
            try:
                await ingest_documents(
                    manager.tools(),
                    settings.docs_dir,
                    manifest_path=settings.docs_manifest_path,
                    chunk_size=settings.docs_chunk_size,
                    chunk_overlap=settings.docs_chunk_overlap,
                )
            except Exception as exc:  # ingestion must never crash the server
                logging.getLogger("redcell.cli").warning("document ingestion failed: %s", exc)

    api = create_app(
        build_agent,
        model_id=settings.model_id,
        api_key=settings.server_api_key,
        gateway=gateway,
        mcp_manager=manager,
        qdrant=qdrant,
        post_startup=post_startup,
        session_store=session_store,
        session_header=settings.session_header,
    )
    bind_host = host or settings.server_host
    bind_port = port or settings.server_port
    typer.echo(f"Serving agent ({settings.model}) as model '{settings.model_id}'.")
    typer.echo(
        f"  security: safety_prompt={'on' if settings.safety_prompt else 'off'}, "
        f"guardrails={'on' if settings.guardrails else 'off'}"
        + (f", denied tools: {', '.join(sorted(denied))}" if denied else "")
    )
    typer.echo(f"  local:  http://127.0.0.1:{bind_port}/v1")
    typer.echo(f"  docker: http://host.docker.internal:{bind_port}/v1  (use this in Open WebUI)")
    if gateway is not None:
        typer.echo(f"  gateway: launching '{settings.gateway_bin}' on :{settings.gateway_port}")
    if qdrant is not None:
        typer.echo(
            f"  qdrant:  docker compose up -d {settings.qdrant_service} "
            f"(RAG store on :{settings.qdrant_port})"
        )
    if post_startup is not None:
        typer.echo(f"  docs:    ingesting PDFs from '{settings.docs_dir}/' into the RAG store")
    uvicorn.run(api, host=bind_host, port=bind_port)


@app.command()
def rag_seed(
    corpus: str = typer.Option(None, "--corpus", help="Corpus JSON path (default: bundled)."),
) -> None:
    """Load the RAG corpus into the store via the running gateway's qdrant-store tool."""
    settings = Settings()
    configure_logging(
        settings.log_level,
        json_logs=settings.log_json,
        log_file=settings.log_file,
        quiet_mcp_transport=settings.log_quiet_mcp_transport,
    )
    path = Path(corpus) if corpus else default_corpus_path()
    docs = load_corpus(path)

    async def _run() -> int:
        async with MCPManager(lambda: streamable_http_session(settings.gateway_url)) as mgr:
            return await rag_seed_corpus(mgr.tools(), docs)

    count = asyncio.run(_run())
    typer.echo(f"seeded {count} docs into the RAG store via {settings.gateway_url}")


@app.command()
def chat(system_prompt: str = typer.Option("You are a helpful assistant.", "--system")) -> None:
    """Start an interactive chat session with a basic agent."""
    settings = Settings()
    configure_logging(
        settings.log_level,
        json_logs=settings.log_json,
        log_file=settings.log_file,
        quiet_mcp_transport=settings.log_quiet_mcp_transport,
    )
    agent = Agent(
        llm=LLM(
            settings.model,
            settings.temperature,
            settings.max_tokens,
            api_base=settings.api_base,
            api_key=settings.api_key,
        ),
        tools=default_tools(settings),
        system_prompt=build_system_prompt(
            system_prompt, safety=settings.safety_prompt, rules=_safety_rules(settings)
        ),
        hooks=logging_hooks(),
        max_iterations=settings.max_iterations,
        guardrail=make_guardrail(settings.guardrails),
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
