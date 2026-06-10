# Configuration reference

All runtime configuration lives in `redcell/config.py` as a Pydantic
`Settings` model. Values are read, in order of precedence:

1. **Environment variables**, prefixed `AGENT_` (e.g. `AGENT_MODEL`).
2. A local **`.env`** file (copy `.env.example` to start).
3. The **defaults** below.

Unknown keys are ignored (`extra="ignore"`), so unrelated env vars are harmless.
Provider credentials such as `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` are **not**
redcell settings — LiteLLM reads them directly from the environment.

The env var for any field is `AGENT_` + the field name upper-cased
(e.g. field `model_id` → `AGENT_MODEL_ID`, `session_ttl_seconds` →
`AGENT_SESSION_TTL_SECONDS`).

## Model & generation

| Env var | Type | Default | Meaning |
|---------|------|---------|---------|
| `AGENT_MODEL` | str | `anthropic/claude-opus-4-8` | LiteLLM model string. Selects the provider/model (see table below). |
| `AGENT_API_BASE` | str? | _(unset)_ | OpenAI-compatible endpoint override for self-hosted servers (vLLM, LM Studio, Ollama-OpenAI). Leave unset for hosted providers. |
| `AGENT_API_KEY` | str? | _(unset)_ | Bearer token sent to `AGENT_API_BASE`. vLLM accepts any non-empty value unless started with `--api-key`. |
| `AGENT_TEMPERATURE` | float | `0.7` | Sampling temperature passed to the model. |
| `AGENT_MAX_TOKENS` | int | `1024` | Max tokens per completion. |
| `AGENT_MAX_ITERATIONS` | int | `25` | Hard cap on tool-call rounds per turn. Prevents infinite tool loops. |
| `AGENT_LLM_MAX_RETRIES` | int | `5` | Retries for transient LLM errors (429/5xx/connection). `0` disables. Honors `Retry-After`; never retries 4xx/auth/validation. |
| `AGENT_LLM_RETRY_BASE_DELAY` | float | `0.5` | Base seconds for exponential backoff (`min(base·2^(n-1), max)` + ≤25% jitter). |
| `AGENT_LLM_RETRY_MAX_DELAY` | float | `30.0` | Cap on a single backoff delay (seconds). |
| `AGENT_LOG_LEVEL` | str | `INFO` | Log level for structlog/stdlib (`DEBUG`/`INFO`/`WARNING`/…). |
| `AGENT_LOG_JSON` | bool | `false` | Render structured events as JSON instead of the console format. |
| `AGENT_LOG_FILE` | str? | _(unset)_ | Write events to this file (append) instead of stderr. With `AGENT_LOG_JSON=true` this is a JSONL event sink per scan. |
| `AGENT_LOG_QUIET_MCP_TRANSPORT` | bool | `true` | Silence the MCP streamable-HTTP transport's benign teardown-race logs (SSE `ClosedResourceError`, `Session termination failed: 202`). Set `false` to keep them when debugging the transport. |

### `AGENT_MODEL` examples

| Provider | `AGENT_MODEL` | Also set |
|----------|---------------|----------|
| Self-hosted vLLM | `hosted_vllm/<model>` | `AGENT_API_BASE` (+ `AGENT_API_KEY`) |
| Local Ollama | `ollama/llama3.1` | — |
| Anthropic | `anthropic/claude-opus-4-8` | `ANTHROPIC_API_KEY` |
| OpenAI | `openai/gpt-4o` | `OPENAI_API_KEY` |

Any [LiteLLM-supported](https://docs.litellm.ai/docs/providers) model string works.

## HTTP server (`redcell serve`)

| Env var | Type | Default | Meaning |
|---------|------|---------|---------|
| `AGENT_SERVER_HOST` | str | `0.0.0.0` | Bind host. Overridable per-run with `--host`. |
| `AGENT_SERVER_PORT` | int | `8800` | Bind port. Overridable per-run with `--port`. |
| `AGENT_SERVER_API_KEY` | str? | _(unset)_ | If set, clients must send `Authorization: Bearer <key>`. Unset = open. |
| `AGENT_MODEL_ID` | str | `redcell` | The model id advertised by `/v1/models` and echoed in responses (the name a client picker shows). |

## Sessions (stateful targets)

See [server-api.md#sessions](server-api.md#sessions) for the full model.

| Env var | Type | Default | Meaning |
|---------|------|---------|---------|
| `AGENT_SESSION_HEADER` | str | `x-redcell-session` | Request header carrying the client-generated session id. A body field `session_id`/`sessionId` is also accepted. |
| `AGENT_SESSION_TTL_SECONDS` | float | `3600.0` | Idle lifetime of a session before eviction. |
| `AGENT_SESSION_MAX` | int | `1000` | Max concurrent sessions; the least-recently-used is evicted past this. |

Sessions are held **in memory only** and lost on restart.

## Security controls

Secure-by-default. Flip any to recreate the deliberately vulnerable target. Full
detail in [security.md](security.md).

| Env var | Type | Default | Off = |
|---------|------|---------|-------|
| `AGENT_SAFETY_PROMPT` | bool | `true` | Bare "helpful assistant" prompt; no refusals/policy. |
| `AGENT_GUARDRAILS` | bool | `true` | No input blocking or output/tool-result redaction. |
| `AGENT_SAFETY_RULES` | str (csv) | _(empty)_ | Subset of named safety rules to include (empty = all): `harm,copyright,truthfulness,commitments,disclosure,fairness`. Isolate rules to measure each one's scan-delta contribution. |
| `AGENT_MCP_TOOL_DENYLIST` | str (csv) | _(empty)_ | All gateway tools enabled. Set e.g. `shell,filesystem` to drop dangerous tools (substring-matched; zero-match terms warn at startup). |

Booleans accept the usual Pydantic forms: `true/false`, `1/0`, `yes/no`.

## Tools

| Env var | Type | Default | Meaning |
|---------|------|---------|---------|
| `AGENT_SEARXNG_URL` | str | `http://127.0.0.1:8989` | Base URL of the SearXNG instance backing the builtin `web_search` tool. |

## AgentGateway

`redcell serve` launches and supervises an AgentGateway process and connects the
agent to its aggregated MCP endpoint. See [tools-and-gateway.md](tools-and-gateway.md).

| Env var | Type | Default | Meaning |
|---------|------|---------|---------|
| `AGENT_GATEWAY_BIN` | str | `agentgateway` | Gateway executable (must be on `PATH`). |
| `AGENT_GATEWAY_CONFIG` | str | `agentgateway/config.yaml` | Gateway config file passed as `-f`. |
| `AGENT_GATEWAY_HOST` | str | `127.0.0.1` | Host for the readiness probe. |
| `AGENT_GATEWAY_PORT` | int | `3030` | Port for the readiness probe. |
| `AGENT_GATEWAY_URL` | str | `http://127.0.0.1:3030/mcp` | The aggregated MCP endpoint the agent connects to. Tune the path (root vs `/mcp`) to match your gateway config. |
| `AGENT_GATEWAY_AUTOSTART` | bool | `true` | If false, `serve` does not spawn the gateway (run it yourself elsewhere). |
| `AGENT_GATEWAY_READY_TIMEOUT` | float | `30.0` | Seconds to wait for the gateway port to accept connections before continuing without it. |

> The field name is `gateway_config_path` but the env var is `AGENT_GATEWAY_CONFIG`
> (the `.env.example` and this table are the source of truth for the env name).

## Qdrant (RAG store)

`redcell serve` brings up a Dockerized Qdrant the same way it launches the gateway —
via `docker compose up -d` — and waits for its REST port before starting the gateway
(so the `rag` target finds a store). Needs Docker; if absent it logs a warning and the
server runs without RAG. See [rag.md](rag.md).

| Env var | Type | Default | Meaning |
| ------- | ---- | ------- | ------- |
| `AGENT_QDRANT_AUTOSTART` | bool | `true` | If false, `serve` does not start Qdrant (run it yourself). |
| `AGENT_QDRANT_COMPOSE_FILE` | str | `docker-compose.yml` | Compose file passed as `-f`. |
| `AGENT_QDRANT_SERVICE` | str | `qdrant` | Compose service name to bring up. |
| `AGENT_QDRANT_HOST` | str | `127.0.0.1` | Host for the readiness probe. |
| `AGENT_QDRANT_PORT` | int | `6333` | Qdrant REST port (readiness probe target). |
| `AGENT_QDRANT_READY_TIMEOUT` | float | `30.0` | Seconds to wait for the port after compose returns. |
| `AGENT_QDRANT_STOP_ON_EXIT` | bool | `false` | If true, `docker compose stop` the service when `serve` exits. Left running by default (persistent data service). |

## Documents (PDF ingestion)

At `serve` startup, PDFs in `docs_dir` are chunked and stored into Qdrant (via the
gateway's `qdrant-store`) so the agent can retrieve them with `qdrant-find`. A hash
manifest skips files already ingested unchanged. See [rag.md](rag.md#auto-ingesting-your-own-pdfs-documents-folder).

| Env var | Type | Default | Meaning |
| ------- | ---- | ------- | ------- |
| `AGENT_DOCS_AUTOLOAD` | bool | `true` | Master switch for startup ingestion. False = skip entirely. |
| `AGENT_DOCS_DIR` | str | `documents` | Folder scanned for top-level `*.pdf` (flat, no recursion). Missing folder = no-op. |
| `AGENT_DOCS_MANIFEST_PATH` | str | `.redcell/ingested.json` | Where the file-hash dedup manifest is stored. |
| `AGENT_DOCS_CHUNK_SIZE` | int | `1000` | Characters per chunk. |
| `AGENT_DOCS_CHUNK_OVERLAP` | int | `150` | Character overlap between adjacent chunks. |

## Full `.env.example`

The repository's [`.env.example`](../.env.example) contains every variable above with
inline comments — copy it to `.env` and edit. Nothing in it is required to start a
chat against a hosted model except `AGENT_MODEL` and the matching provider key.

## Programmatic configuration

When embedding redcell as a library, construct `Settings()` (it still reads env/`.env`)
or pass values directly to the components — `Agent`, `LLM`, `create_app`, `SessionStore`,
`make_guardrail`, `build_system_prompt` all take plain arguments and do not require the
`Settings` object. See [development.md](development.md).
