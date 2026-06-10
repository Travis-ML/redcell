# Architecture

redcell is a small, layered Python package. Each module has one job and a narrow
interface, so providers, tools, memory, and guardrails are swappable without
touching the agent loop.

## Components

| Module | Responsibility |
|--------|----------------|
| `redcell/agent.py` | The async tool-use loop. Owns turn orchestration, guardrail screening, memory persistence. |
| `redcell/llm.py` | The **only** module that imports a provider SDK (`litellm`). Normalizes any provider response to `LLMResponse`. |
| `redcell/tools.py` | `Tool`, the `@tool` decorator (schema from type hints), and `ToolRegistry` (executes by name, catches errors as strings). |
| `redcell/mcp.py` | `MCPManager` — discovers tools from one upstream MCP endpoint (AgentGateway) and wraps each as a local `Tool`. |
| `redcell/gateway.py` | `GatewaySupervisor` — spawns and health-checks the AgentGateway child process. |
| `redcell/qdrant.py` | `QdrantSupervisor` — brings the RAG store up via `docker compose` and health-checks its port. |
| `redcell/server.py` | FastAPI app exposing the OpenAI-compatible `/v1/*` endpoints. |
| `redcell/sessions.py` | `SessionStore` — server-side conversation memory keyed by session id (LRU + TTL). |
| `redcell/memory.py` | `Memory` interface + `InMemoryStore` (per-conversation message history). |
| `redcell/prompts.py` | `BASE_SYSTEM_PROMPT` + the optional `SAFETY_POLICY`. |
| `redcell/guardrails.py` | `Guardrail` protocol + `PatternGuardrail`/`NullGuardrail`. |
| `redcell/observability.py` | `Hooks` (lifecycle events) + structured logging. |
| `redcell/searxng.py` | The builtin `web_search` tool over a SearXNG instance. |
| `redcell/rag/` | RAG corpus loading, seeding, and startup **PDF document ingestion** (`documents.py`) through the gateway's `qdrant-store`. |
| `redcell/config.py` | `Settings` — typed config from env / `.env`. |
| `redcell/cli.py` | Typer CLI wiring everything together. |

## Design principles

- **Vendor-agnostic.** Switching models is one config value (`AGENT_MODEL`); only
  `llm.py` knows a vendor exists.
- **Stateless core, optional state.** The HTTP server builds a fresh `Agent` per
  request. History is client-owned by default; server-side `SessionStore` is opt-in.
- **Resilient/degrading.** A missing gateway binary, an unreachable MCP endpoint, or
  a failing tool call never crashes a turn — they log and the agent continues with
  whatever tools are available, surfacing tool errors back to the model as strings.
- **Observable.** Every LLM call, tool call, and guardrail action emits a `Hooks`
  event tagged with a per-turn `run_id` (the session id for stateful turns), so a
  turn's events stay attributable even when many sessions interleave. `tool_end`
  carries the call `duration_ms` and the (truncated) tool result, making injection
  success — e.g. a RAG canary surfacing in a tool's output — visible in the event
  stream itself. Set `AGENT_LOG_JSON=true` (+ `AGENT_LOG_FILE`) for a JSONL event
  sink per scan. Every MCP tool call also routes through the gateway choke point.
- **Secure-by-default, toggleable.** Safety prompt and guardrail are on; each
  vulnerable surface is a single config switch (see [security.md](security.md)).

## Request lifecycle (served API)

1. Client POSTs `/v1/chat/completions` with OpenAI-shaped `messages`.
2. `server.py` authenticates (if `AGENT_SERVER_API_KEY` set), then resolves a session
   id from the configured header / body field (if `SessionStore` enabled).
3. `agent_factory()` builds a fresh `Agent` (builtin tools + non-denied gateway tools,
   the configured system prompt, and the active guardrail).
4. **Stateless path** (`run_messages`) or **stateful path** (`run_session`) runs.
5. The agent screens input, runs the loop, screens/redacts output, returns a `ChatResult`.
6. `server.py` serializes it as a `chat.completion` (or streams `chat.completion.chunk`s).

## The agent tool-use loop (`Agent._run_loop`)

```
prepend system prompt ─▶ ┌─────────────────────────────────────────────┐
                         │ call LLM(messages, tool specs)  [retried]   │ ◀── repeats up to
                         │   ├ no tool calls ─▶ final text ─▶ return    │     max_iterations
                         │   └ tool calls:                              │
                         │       append assistant turn                 │
                         │       run tools (read-only parallel,        │
                         │         mutating serial) → results          │
                         │       append each tool result               │
                         └─────────────────────────────────────────────┘
```

- **LLM calls retry** transient errors (429/5xx/connection) with exponential
  backoff + jitter, honoring `Retry-After`; 4xx/auth/validation errors fail fast
  (`AGENT_LLM_MAX_RETRIES`, default `5`).
- **Tool execution is partitioned**: consecutive concurrency-safe (read-only)
  calls run in parallel under a bounded pool; a mutating call runs alone as a
  barrier, so a same-turn write+read (or two writes) can't race. Result order
  always matches call order. Tools declare `read_only`/`concurrency_safe` (bool or
  per-argument predicate); the defaults are fail-closed (assume mutating/serial).
  Known read-only MCP tools (`fetch`, `qdrant-find`, filesystem reads, `grep`) are
  flagged parallel-safe at assembly via `AGENT_MCP_READONLY_TOOLS`; mutating MCP
  tools (shell, writes, `qdrant-store`) stay serial.
- Tool results are returned as a `ToolResult(content, is_error)`. Failures (unknown
  tool, raised exception) are flagged `is_error` and wrapped in `<tool_use_error>…</tool_use_error>`
  so the model reliably recognizes them; all results are head/tail truncated to
  bound the context window.
- The loop is bounded by `max_iterations` (Agent default `10`; the CLI/server pass
  `AGENT_MAX_ITERATIONS`, default `25`). Hitting the cap returns a "Stopped: reached
  max_iterations" message rather than looping forever.
- A separate **reasoning channel** (`LLMResponse.reasoning`) is preserved when the
  provider exposes one (e.g. vLLM reasoning parsers, surfaced as `reasoning_content`).

## Entry points into the agent

| Method | Used by | History ownership |
|--------|---------|-------------------|
| `Agent.run(text)` | CLI `chat` REPL, `examples/demo_agent.py` | Agent's own `memory` |
| `Agent.run_messages(messages)` | Server, stateless requests | Caller (client) owns it |
| `Agent.run_session(memory, incoming)` | Server, stateful requests | Server-side `SessionStore` |

All three apply the guardrail: input is screened before the LLM is called, and the
final text is screened/redacted before return. See [security.md](security.md).
