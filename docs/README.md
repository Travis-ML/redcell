# redcell documentation

redcell is a local-first **security-testing agent**: a realistic, observable agent
*target* you point AI-security tools at (Garak, Promptfoo, llm-guard, …). It drives
any model through [LiteLLM], exposes a broad MCP toolset proxied through
[AgentGateway], and serves an OpenAI-compatible HTTP API — so a scanner can attack
it, a guardrail can wrap it, and every tool call routes through one observable
choke point.

This folder is the full reference. Start with whichever page matches your task.

## Contents

| Page | What it covers |
| ---- | -------------- |
| [Architecture](architecture.md) | Components, the request lifecycle, the agent tool-use loop, design principles |
| [Configuration reference](configuration.md) | Every `AGENT_*` setting — type, default, meaning |
| [CLI reference](cli.md) | `redcell serve`, `chat`, `rag-seed`, `doctor`, `version` |
| [Server & API](server-api.md) | The OpenAI-compatible endpoints, auth, streaming, sessions |
| [Sessions](server-api.md#sessions) | Stateful vs stateless, session ids, promptfoo wiring |
| [Security controls](security.md) | Safety prompt, guardrails, tool denylist, threat model, eval workflow |
| [Tools & AgentGateway](tools-and-gateway.md) | Builtin tools, the `@tool` decorator, MCP, the gateway targets |
| [RAG knowledge base](rag.md) | Qdrant, the seed corpus, poisoning/canaries, indirect injection |
| [Observability](observability.md) | Structured logs, opt-in OpenTelemetry tracing, Grafana/Tempo/Loki |
| [Development](development.md) | Layout, tests, public API, extending tools/guardrails/memory |

## 60-second start

```bash
uv sync
cp .env.example .env          # set AGENT_MODEL + the matching key/endpoint
uv run redcell chat           # interactive REPL
# or
uv run redcell serve          # OpenAI-compatible API on :8800 (+ AgentGateway)
```

Or bring up the whole stack — AgentGateway, SearXNG, Qdrant, the OpenShell sandbox
gateway, and redcell — in one shot:

```bash
docker compose up -d   # builds the redcell and sandbox images on first run
```

## Mental model

```text
┌───────────────────────────────┐
│ Client / scanner              │
│ Promptfoo, Garak, Open WebUI  │
│ curl, OpenAI SDK              │
└───────────────┬───────────────┘
                │  HTTP   /v1/chat/completions
                ▼
┌───────────────────────────────┐
│ redcell  (FastAPI server)     │
│ Agent tool-use loop           │
│    ├ LLM  (LiteLLM)           │
│    ├ tools                    │
│    ├ guardrail                │
│    └ safety prompt            │
└───────────────┬───────────────┘
                │  MCP    :3030
                ▼
┌────────────────────────────────────┐
│ AgentGateway  (choke point)        │
│    ├ playwright                    │
│    ├ filesystem  (OpenShell sbx)   │
│    ├ fetch                         │
│    ├ rag  (Qdrant)                 │
│    └ shell        (OpenShell sbx)  │
└────────────────────────────────────┘
```

`redcell`, AgentGateway's local targets, Qdrant, and SearXNG's containment for
`filesystem`/`shell` are all reachable this way whether redcell runs on the host or
inside `docker compose up`'s `redcell` service — only the network names change
(`qdrant` vs `127.0.0.1`, etc., see [configuration.md](configuration.md)). The
OpenShell sandbox itself is a sibling container the OpenShell gateway creates on
demand, not shown as its own box above; see
[tools-and-gateway.md](tools-and-gateway.md#setting-up-the-execution-sandbox).

Two layers you toggle for testing: **security controls** (safety prompt + guardrail,
on by default — see [security.md](security.md)) and the **vulnerable tool surface**
(the gateway targets, deny individual ones via config). A third, opt-in layer —
**tracing** — records everything the agent did for forensic replay; see
[observability.md](observability.md).

[LiteLLM]: https://docs.litellm.ai/
[AgentGateway]: https://agentgateway.dev/
