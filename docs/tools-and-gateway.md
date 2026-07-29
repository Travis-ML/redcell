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
- **`ToolRegistry`** holds tools and executes by name, returning a
  `ToolResult(content, is_error)`. Unknown tools and raised exceptions are flagged
  `is_error` and wrapped in `<tool_use_error>…</tool_use_error>` (never raised); all
  results are head/tail truncated to bound the context window. Tools carry
  fail-closed `read_only`/`concurrency_safe`/`destructive` classification (the agent
  runs read-only calls in parallel, mutating calls serially) and a `source`
  (`builtin`/`mcp`); a builtin shadows a same-named MCP tool.
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
| `filesystem` | `mcp-server-filesystem` over **SSH** to the OpenShell sandbox | Read/write/edit/list, scoped to `/sandbox` **inside the sandbox**. |
| `fetch` | `mcp-server-fetch` (uvx, pinned to `mcp<2`) | HTTP fetch. |
| `rag` | `mcp-server-qdrant` (uvx) | `qdrant-store` (write/poison) + `qdrant-find` (retrieval). See [rag.md](rag.md). |
| `shell` | `mcp-server-commands` over **SSH** to the OpenShell sandbox | `run_process`, confined to the sandbox. |

`filesystem` and `shell` run **inside an OpenShell sandbox over SSH**, so file and
command operations can only ever execute in that contained environment — never on the
gateway host. Containment is declared in `sandbox/policy.yaml` and enforced with
Landlock and seccomp; see [the sandbox README](../sandbox/README.md). The config also
sets permissive CORS (`allowOrigins: *`) so browser-based scanners can connect.

The `mcp<2` pin on `fetch` is load-bearing. mcp 2.0 renamed `McpError` to `MCPError`
and `mcp-server-fetch` still imports the old name, so an unpinned resolve crashes it on
import — and AgentGateway reports a failed stdio target as a 500 on the *aggregated*
endpoint, which leaves **every** target with zero tools. If you ever see all five
targets at `0`, suspect one broken server rather than five.

You own `agentgateway/config.yaml` — add targets, policies, auth, and observability
there for the tools you want to exercise.

### Setting up the execution sandbox

`filesystem` and `shell` run inside an [OpenShell](https://github.com/NVIDIA/OpenShell)
sandbox. `redcell serve` creates it and writes the SSH config the gateway launches the
servers through, so there is nothing to hand-maintain in `~/.ssh/config` and no key
material anywhere — the OpenShell gateway authenticates the tunnel.

Two one-time steps:

1. **Install the CLI and build the sandbox image:**
   ```bash
   uv tool install -U openshell
   docker build -t redcell-sandbox:local sandbox/
   ```

2. **Start the OpenShell gateway** (a container; config in `openshell/gateway.toml`).
   It must be published on host port **8080 specifically** — the docker compute driver
   tells sandboxes to call back on the gateway's own port, so remapping it leaves them
   stuck in `Provisioning`. Its JWT signing keys are generated by
   `openshell/jwt-init.sh`; the gateway refuses to start without them and does not
   create them itself.

Then `redcell serve` reuses the sandbox if it exists and creates it from
`sandbox/policy.yaml` if it doesn't. Everything degrades the way the rest of the stack
does: if the CLI is missing or the gateway is down, those two tools error and everything
else (Playwright, Fetch, RAG, builtins) still works. The agent never falls back to
running commands on your host.

Verify containment directly — this is worth doing after any change to the image or
policy, because a policy that is too permissive fails silently:

```bash
openshell sandbox exec -n redcell-sbx -- id                 # uid=1000(sandbox)
openshell sandbox exec -n redcell-sbx -- touch /etc/nope    # must be denied
openshell sandbox exec -n redcell-sbx -- getent hosts example.com   # no egress
```

To widen or tighten what the sandbox can do, edit `sandbox/policy.yaml` — see
[the sandbox README](../sandbox/README.md) for which parts are hot-reloadable and which
need the sandbox recreated.

**Why SSH and not `openshell sandbox exec`** for the MCP transport, since `exec` looks
like the obvious choice: `exec` buffers stdin until EOF, so a long-lived JSON-RPC
session over it never gets a reply — measured on 0.0.92, and `--tty` does not help.
SSH streams in both directions. If you switch this, test with stdin held open.

### Disabling dangerous tools

Drop tools before the agent can call them with `AGENT_MCP_TOOL_DENYLIST` (comma-
separated tool names, matched against the gateway-exposed names shown in `serve` logs):

```bash
AGENT_MCP_TOOL_DENYLIST=shell,filesystem uv run redcell serve
```

Prerequisites for the gateway: `agentgateway` on `PATH`, plus `npx` (Node) and `uvx`
for the stdio backends, and a running OpenShell gateway plus an `ssh` client for
`filesystem`/`shell`.

## Prerequisites summary

| Capability | Needs |
|------------|-------|
| Builtin `web_search` | a SearXNG instance at `AGENT_SEARXNG_URL` |
| MCP tools | `agentgateway` + `npx` + `uvx` |
| `filesystem` / `shell` | `openshell` + `ssh`, and the OpenShell gateway running |
| `rag` tools | Qdrant running (`docker compose up -d qdrant`) |

Run **`redcell doctor`** to check these runtimes before starting, and watch the **per-target
tool health** line `serve` logs once the gateway connects — a target showing `0` tools means
its MCP server didn't start (missing runtime or unreachable VM). See
[cli.md](cli.md#redcell-doctor).
