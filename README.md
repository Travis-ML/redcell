# redcell

A local-first **security testing agent**: a realistic, observable agent target
for evaluating open-source AI-security tools (Garak, Promptfoo, llm-guard, …).

redcell drives any model — local (vLLM/Ollama) or cloud (Anthropic/OpenAI) via
[LiteLLM] — with a broad MCP toolset proxied through [AgentGateway], and exposes
an OpenAI-compatible HTTP endpoint. Point a scanner at the endpoint, wrap a
guardrail around it, and watch every tool call route through the gateway choke
point for a full trace of what the agent actually did.

## Features

- **Local-first, cloud-capable** — one config value (`AGENT_MODEL`) switches
  between self-hosted vLLM/Ollama and hosted Anthropic/OpenAI.
- **MCP tools via AgentGateway** — Playwright (browser), Filesystem, and Fetch
  behind a single aggregated endpoint; add more by editing one YAML.
- **OpenAI-compatible server** — `redcell serve` exposes `/v1/chat/completions`
  so Open WebUI and scanners like Garak/Promptfoo can drive it.
- **Observable** — every MCP tool call routes through the gateway, so you can
  confirm whether an attack actually fired.
- **Batteries included** — `@tool` decorator, conversation memory, typed config,
  structured logging, async tool-use loop.

## Documentation

Full reference lives in [`docs/`](docs/README.md):

- [Architecture](docs/architecture.md) — components, request lifecycle, the agent loop
- [Configuration reference](docs/configuration.md) — every `AGENT_*` setting
- [CLI reference](docs/cli.md) — `serve`, `chat`, `rag-seed`, `version`
- [Server & API](docs/server-api.md) — endpoints, auth, streaming, **sessions**
- [Security controls](docs/security.md) — safety prompt, guardrails, toggles, eval workflow
- [Tools & AgentGateway](docs/tools-and-gateway.md) — builtin tools, MCP, gateway targets
- [RAG knowledge base](docs/rag.md) — Qdrant, corpus, poison/canaries
- [Development](docs/development.md) — layout, tests, public API, extension points

## Quickstart

```bash
uv sync
cp .env.example .env      # then set AGENT_MODEL + keys/endpoint
uv run redcell chat
```

## Models (local + cloud)

Set `AGENT_MODEL` (LiteLLM format) and the matching key/endpoint:

| Provider | `AGENT_MODEL` | Needs |
|----------|---------------|-------|
| Self-hosted vLLM | `hosted_vllm/<model>` | `AGENT_API_BASE` (+ `AGENT_API_KEY`) |
| Local Ollama | `ollama/llama3.1` | — |
| Anthropic | `anthropic/claude-opus-4-8` | `ANTHROPIC_API_KEY` |
| OpenAI | `openai/gpt-4o` | `OPENAI_API_KEY` |

## Serve as an OpenAI-compatible API

```bash
uv run redcell serve      # binds 0.0.0.0:8800
```

Serves `GET /v1/models` and `POST /v1/chat/completions` (streaming + not). Point
any OpenAI-compatible client at `http://<host>:8800/v1`:

- **Open WebUI** (Docker): Base URL `http://host.docker.internal:8800/v1`, any API key.
- **Garak / Promptfoo**: target `http://127.0.0.1:8800/v1/chat/completions`, model `redcell`.

### Sessions (stateful vs stateless)

By default the server is **stateless**: every request runs only the messages it
carries, so the client owns history (promptfoo wizard: *"No — resend the full
interaction history"*). Leave it as-is and multi-turn just works.

To run redcell as a **stateful** target — promptfoo sends only the new turn plus a
session id, and redcell remembers the rest — send a session id on each request via
the `x-redcell-session` header (or a `session_id`/`sessionId` body field). History
is keyed off that id and held in memory (idle-TTL + LRU eviction; lost on restart;
tuned by `AGENT_SESSION_*`). No session id → unchanged stateless behavior.

In promptfoo's red-team target wizard answer: *Remembers history → **Yes***,
*Session management → **Client-generated Session ID***, *Session ID Extraction →
**(leave empty)***. The generated target wires the id into each request:

```yaml
targets:
  - id: openai:chat:redcell
    config:
      apiBaseUrl: http://127.0.0.1:8800/v1
      apiKey: redcell            # any value unless AGENT_SERVER_API_KEY is set
      headers:
        x-redcell-session: '{{sessionId}}'
defaultTest:
  options:
    transformVars: '{ ...vars, sessionId: context.uuid }'   # one id per test case
```

### Security controls (secure-by-default, toggleable)

redcell ships defensive controls **on by default**, with each vulnerable surface
exposed as a single toggle so you can baseline the deliberately-unguarded target
and then measure the delta with controls on:

| Control | Env | Default | Off = vulnerable behavior |
|---------|-----|---------|---------------------------|
| Safety system prompt | `AGENT_SAFETY_PROMPT` | `true` | bare "helpful assistant", no refusals |
| Input/output guardrail | `AGENT_GUARDRAILS` | `true` | no moderation/redaction |
| Dangerous MCP tools | `AGENT_MCP_TOOL_DENYLIST` | *(none)* | e.g. `shell,filesystem` removes the worst exfiltration surface |

- The **safety policy** (`redcell/prompts.py`) refuses harmful/illegal/copyright
  requests, forbids fabricated tool results and binding commitments, and bars
  disclosure of internal architecture — the prompt-level fixes for the scan findings.
- The **guardrail** (`redcell/guardrails.py`) is a dependency-free baseline that
  blocks a few high-signal harmful inputs and redacts PII + internal identifiers
  (backends, sandbox paths) from output. It implements a small `Guardrail`
  protocol, so swapping in llm-guard, Llama Guard, or an LLM self-check for
  semantic moderation is a drop-in — no other code changes.

To recreate the original vulnerable target end-to-end:
`AGENT_SAFETY_PROMPT=false AGENT_GUARDRAILS=false uv run redcell serve`.

## MCP tools via AgentGateway

`serve` launches a local [AgentGateway] process (`agentgateway -f
agentgateway/config.yaml`) and connects the agent to its aggregated MCP
endpoint. The starter config wires **Playwright**, **Filesystem** (scoped to
`agentgateway/sandbox/`), and **Fetch** behind `:3030` (UI on `:15000`).

Prerequisites: `agentgateway` on your PATH, plus `npx` (Node) and `uvx`. If the
gateway can't start, `serve` runs with builtin tools only. Every MCP tool call
routes through the gateway — the choke point that makes redcell a useful test
subject.

## RAG knowledge base (Qdrant)

redcell ships an enterprise-standard RAG surface: a self-hosted **Qdrant** vector
DB behind the official **`mcp-server-qdrant`** (local FastEmbed embeddings),
exposed as the gateway `rag` target with `qdrant-store` and `qdrant-find`.

```bash
docker compose up -d qdrant     # start Qdrant on :6333
uv run redcell serve            # brings up the gateway + rag target
uv run redcell rag-seed         # load the bundled corpus into the store
```

The bundled corpus (`redcell/rag/corpus/seed_corpus.json`) mixes benign docs with
**planted poison docs** carrying unique canary IDs. Because retrieval routes
through the gateway, you can see whether a retrieved poison doc actually drove a
`shell`/`filesystem` action — the canary appearing in a tool call (or a
`RC-CANARY-*.txt` file in the VM sandbox) is measurable injection success. This is
the **indirect prompt injection** surface for tools like Garak/Promptfoo to probe.

## Development

```bash
uv run pytest            # tests (offline; never hit the network)
uv run ruff check .      # lint
uv run ruff format .     # format
```

## License

[Prosperity Public License 3.0.0](LICENSE) © Streamline AI LLC (dba TravisML.ai)

Free for noncommercial use. Commercial use is allowed for a thirty-day trial;
beyond that, contact the contributor for a commercial license. This is a
source-available license, not an OSI open-source license.

[LiteLLM]: https://docs.litellm.ai/
[AgentGateway]: https://agentgateway.dev/
