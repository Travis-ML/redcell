# Development

## Project layout

```
redcell/
  agent.py          # tool-use loop, guardrail screening, memory persistence
  llm.py            # LiteLLM wrapper (only provider-aware module)
  tools.py          # Tool, @tool, ToolRegistry
  mcp.py            # MCPManager — gateway tools as local Tools
  gateway.py        # GatewaySupervisor — child process lifecycle
  server.py         # FastAPI OpenAI-compatible app
  sessions.py       # SessionStore (LRU + TTL)
  memory.py         # Memory interface + InMemoryStore
  prompts.py        # BASE_SYSTEM_PROMPT + SAFETY_POLICY
  guardrails.py     # Guardrail protocol + Pattern/Null guardrails
  observability.py  # Hooks + structured logging
  searxng.py        # web_search tool
  config.py         # Settings (env / .env)
  cli.py            # Typer CLI
  rag/              # corpus loading + seeding
agentgateway/config.yaml   # MCP backends aggregated by the gateway
docker-compose.yml         # Qdrant for RAG
examples/demo_agent.py     # minimal library usage
tests/                     # offline test suite (never hits the network)
docs/                      # this documentation
```

## Setup & common commands

```bash
uv sync                         # install deps (incl. dev group)
uv run pytest                   # run the test suite (offline)
uv run pytest -q tests/test_agent.py
uv run ruff check redcell tests # lint
uv run ruff format redcell tests
uv run python examples/demo_agent.py
```

- Python `>=3.11`. Ruff: line length 100, rules `E,F,I,UP,B`.
- Tests use `pytest` with `asyncio_mode = "auto"` (async tests need no decorator) and a
  `StubLLM` (`tests/conftest.py`) that replays scripted `LLMResponse`s, so the suite is
  fully offline and deterministic. There is one test module per source module.

## Public API

The package re-exports its stable surface from `redcell/__init__.py`:

```python
from redcell import (
    Agent, ChatResult,            # the agent + its result type
    LLM, LLMResponse, ToolCall,   # model layer
    Tool, ToolRegistry, tool,     # tools
    Memory, InMemoryStore,        # memory
    Hooks, configure_logging, logging_hooks,  # observability
    make_web_search,              # builtin tool factory
    MCPManager, GatewaySupervisor,# MCP + gateway
    create_app,                   # the FastAPI app factory
    Settings,                     # config
)
```

`build_system_prompt`/`SAFETY_POLICY` (`redcell.prompts`), the guardrails
(`redcell.guardrails`), and `SessionStore` (`redcell.sessions`) are imported from their
modules directly.

## Minimal library usage

See `examples/demo_agent.py`:

```python
from redcell import LLM, Agent, ToolRegistry, tool, logging_hooks

@tool
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b

agent = Agent(
    llm=LLM("anthropic/claude-opus-4-8"),
    tools=ToolRegistry([add]),
    system_prompt="You are concise.",
    hooks=logging_hooks(),
)
print(await agent.run("What is 21 + 21?"))
```

## Extending

### Add a builtin tool

Decorate a function with `@tool` and register it (e.g. extend `default_tools` in
`cli.py`). Type hints generate the schema; the docstring is the description. Return a
string, or any JSON-serializable value (it is `json.dumps`-ed). Raised exceptions are
caught and surfaced to the model as an `is_error` `ToolResult` wrapped in
`<tool_use_error>…</tool_use_error>`. Declare effects for safe parallelism, e.g.
`@tool(read_only=True, concurrency_safe=True)` (both accept a bool or a per-argument
predicate); the defaults are fail-closed (assumed mutating, run serially).

### Custom guardrail

Implement the `Guardrail` protocol (two methods) and pass it to the agent — this is the
seam for plugging in llm-guard, Llama Guard, or an LLM self-check. The methods are
**async**, so a network-backed moderator can `await` its API without blocking the
agent's event loop (a pure in-process check just doesn't await anything):

```python
from redcell.guardrails import Verdict

class MyGuardrail:
    async def check_input(self, text: str) -> Verdict:
        # await your moderation API here; return allowed=False to block
        return Verdict(allowed=True, text=text)

    async def check_output(self, text: str) -> Verdict:
        # return Verdict(allowed=True, text=<redacted>, reason="…") to rewrite
        return Verdict(allowed=True, text=text)

agent = Agent(llm=..., guardrail=MyGuardrail())
```

`check_input` runs before the LLM (a block short-circuits to a refusal `ChatResult`);
`check_output` runs on the final text. Both fire across `run`, `run_messages`, and
`run_session`. Guardrail actions emit `guardrail_input_block` /
`guardrail_output_redact` hooks.

### Custom memory backend

Implement the `Memory` interface (`append`, `load`, `clear`) — e.g. Redis, a database,
or a summarizing window — and pass it as `Agent(memory=…)`, or have `SessionStore` hand
out instances of it per session (the store currently creates `InMemoryStore`s).

### Custom model provider

Change `AGENT_MODEL` to any LiteLLM model string; for self-hosted endpoints set
`AGENT_API_BASE`/`AGENT_API_KEY`. No code change — `llm.py` is the only provider-aware
module.

### Observability hooks

`Hooks` fires on `llm_start`, `llm_end`, `tool_start`, `tool_end`,
`guardrail_input_block`, `guardrail_output_redact`. Register callbacks with
`hooks.on(event, cb)` or use `logging_hooks()` to log the standard lifecycle events.
