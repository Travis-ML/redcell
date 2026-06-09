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
| `AGENT_LOG_LEVEL` | str | `INFO` | Log level for structlog/stdlib (`DEBUG`/`INFO`/`WARNING`/…). |

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
| `AGENT_GUARDRAILS` | bool | `true` | No input blocking or output redaction. |
| `AGENT_MCP_TOOL_DENYLIST` | str (csv) | _(empty)_ | All gateway tools enabled. Set e.g. `shell,filesystem` to drop dangerous tools. |

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

## Full `.env.example`

The repository's [`.env.example`](../.env.example) contains every variable above with
inline comments — copy it to `.env` and edit. Nothing in it is required to start a
chat against a hosted model except `AGENT_MODEL` and the matching provider key.

## Programmatic configuration

When embedding redcell as a library, construct `Settings()` (it still reads env/`.env`)
or pass values directly to the components — `Agent`, `LLM`, `create_app`, `SessionStore`,
`make_guardrail`, `build_system_prompt` all take plain arguments and do not require the
`Settings` object. See [development.md](development.md).
