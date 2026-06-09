# Tools & AgentGateway

redcell gives the model two kinds of tools: a few **builtin** Python tools and a broad
set of **MCP tools** aggregated behind AgentGateway. Every tool — builtin or remote —
is a `redcell.tools.Tool` and is invoked the same way by the agent loop.

## The `Tool` abstraction (`redcell/tools.py`)

A `Tool` wraps a callable plus a JSON schema and a description.

- **`@tool`** decorator: turns a plain function into a `Tool`. The JSON schema is
  derived from the parameter type hints; the description is the docstring.
  ```python
  from redcell.tools import tool

  @tool
  def add(a: float, b: float) -> float:
      """Add two numbers and return the sum."""
      return a + b
  ```
  Supported type hints map to JSON types: `int→integer`, `float→number`,
  `str→string`, `bool→boolean`, `list→array`, `dict→object` (anything else → `string`).
  Parameters without a default are marked `required`.
- Remote tools (MCP) construct `Tool` directly with the upstream `name`,
  `description`, and `inputSchema`.
- **`ToolRegistry`** holds tools and executes by name. Unknown tools and raised
  exceptions are returned as `"Error: …"` strings the model can read — never raised.
- Sync functions run in a thread (`asyncio.to_thread`); async functions are awaited.

## Builtin tools

Registered for both `chat` and `serve` (`default_tools` in `cli.py`):

| Tool | Module | What it does |
|------|--------|--------------|
| `add(a, b)` | `cli.py` | Adds two numbers (demo tool). |
| `utc_now()` | `cli.py` | Returns the current UTC time, ISO 8601. |
| `web_search(query, max_results=5)` | `searxng.py` | Web search via a self-hosted **SearXNG** instance (`AGENT_SEARXNG_URL`). Returns ranked title/URL/snippet lines. |

`web_search` needs a reachable SearXNG with JSON output enabled; otherwise the call
returns an error string.

## MCP tools via AgentGateway

`redcell serve` launches a local [AgentGateway](https://agentgateway.dev/) process
(`agentgateway -f agentgateway/config.yaml`) and connects the agent to its single
aggregated MCP endpoint (`AGENT_GATEWAY_URL`, default `http://127.0.0.1:3030/mcp`).
AgentGateway is the **observable choke point**: every MCP tool call passes through it.

`MCPManager` (`redcell/mcp.py`) discovers the gateway's tools once at startup and wraps
each as a local `Tool`. Each tool *call* opens its own short-lived MCP session (connect
→ call → close), which keeps the protocol's session/SSE semantics correct under the web
server's per-request tasks.

**Resilience:** if the gateway binary is missing, never becomes ready, or the MCP
endpoint is unreachable, discovery yields **zero** MCP tools and the agent runs with
builtins only. A single failing tool call returns an error string, not an exception.

### Gateway targets (`agentgateway/config.yaml`)

The starter config aggregates these MCP backends behind `:3030` (UI on `:15000`):

| Target | Backend | Notes |
|--------|---------|-------|
| `playwright` | `@playwright/mcp` (npx) | Browser automation. |
| `filesystem` | `mcp-server-filesystem` over **SSH** to a Debian VM | Read/write/edit/list, scoped to `/home/redcell/sandbox` **on the VM**. |
| `fetch` | `mcp-server-fetch` (uvx) | HTTP fetch. |
| `rag` | `mcp-server-qdrant` (uvx) | `qdrant-store` (write/poison) + `qdrant-find` (retrieval). See [rag.md](rag.md). |
| `shell` | `mcp-server-commands` over **SSH** to a Debian VM | `run_command` / `run_script`, confined to the VM. |

`filesystem` and `shell` run **on a dedicated Debian VM over SSH** (the `debian-agent`
host alias lives in the operator's `~/.ssh/config`, intentionally out of the repo), so
file/command operations can only ever execute in that contained environment — never on
the gateway host. Harden the VM (host-only/NAT networking, non-root user,
snapshot-before-use). The config also sets permissive CORS (`allowOrigins: *`) so
browser-based scanners can connect.

You own `agentgateway/config.yaml` — add targets, policies, auth, and observability
there for the tools you want to exercise.

### Disabling dangerous tools

Drop tools before the agent can call them with `AGENT_MCP_TOOL_DENYLIST` (comma-
separated tool names, matched against the gateway-exposed names shown in `serve` logs):

```bash
AGENT_MCP_TOOL_DENYLIST=shell,filesystem uv run redcell serve
```

Prerequisites for the gateway: `agentgateway` on `PATH`, plus `npx` (Node) and `uvx`
for the stdio backends, and a reachable `debian-agent` SSH host for `filesystem`/`shell`.

## Prerequisites summary

| Capability | Needs |
|------------|-------|
| Builtin `web_search` | a SearXNG instance at `AGENT_SEARXNG_URL` |
| MCP tools | `agentgateway` + `npx` + `uvx` |
| `filesystem` / `shell` | SSH access to the `debian-agent` VM |
| `rag` tools | Qdrant running (`docker compose up -d qdrant`) |
