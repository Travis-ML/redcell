# Configuration reference

All runtime configuration lives in `redcell/config.py` as a Pydantic
`Settings` model. Values are read, in order of precedence:

1. **Environment variables**, prefixed `AGENT_` (e.g. `AGENT_MODEL`).
2. A local **`.env`** file (copy `.env.example` to start).
3. The **defaults** below.

Unknown keys are ignored (`extra="ignore"`), so unrelated env vars are harmless.
A blank value (`AGENT_SERVER_API_KEY=`) counts as unset, so the blank placeholders in
`.env.example` leave each setting at its default.
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
| `AGENT_CONTEXT_WINDOW` | int | `0` | Model context window in tokens. `0` disables auto-compaction; set it to your model's window (e.g. `8192`) so long runs don't overflow — old turns are summarized, a recent tail kept verbatim, and a real overflow triggers a compact-and-retry. |
| `AGENT_COMPACT_RATIO` | float | `0.8` | Fraction of the window at which compaction kicks in. |
| `AGENT_LLM_MAX_RETRIES` | int | `5` | Retries for transient LLM errors (429/5xx/connection). `0` disables. Honors `Retry-After`; never retries 4xx/auth/validation. |
| `AGENT_LLM_RETRY_BASE_DELAY` | float | `0.5` | Base seconds for exponential backoff (`min(base·2^(n-1), max)` + ≤25% jitter). |
| `AGENT_LLM_RETRY_MAX_DELAY` | float | `30.0` | Cap on a single backoff delay (seconds). |
| `AGENT_LOG_LEVEL` | str | `INFO` | Log level for structlog/stdlib (`DEBUG`/`INFO`/`WARNING`/…). |
| `AGENT_LOG_JSON` | bool | `false` | Render structured events as JSON instead of the console format. |
| `AGENT_LOG_FILE` | str? | _(unset)_ | Write events to this file (append) instead of stderr. With `AGENT_LOG_JSON=true` this is a JSONL event sink per scan. |
| `AGENT_LOG_QUIET_MCP_TRANSPORT` | bool | `true` | Silence the MCP streamable-HTTP transport's benign teardown-race logs (SSE `ClosedResourceError`, `Session termination failed: 202`). Set `false` to keep them when debugging the transport. |
| `AGENT_SCORECARD` | bool | `true` | Emit a per-run `scorecard` event (tokens, USD cost, llm/tool/guardrail counts, per-model breakdown) at each request's end. Pricing table is `redcell/pricing.py`; local/unknown models count tokens at $0. |

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
| `AGENT_MCP_READONLY_TOOLS` | str (csv) | _(empty)_ | MCP tools (name substring, case-insensitive) to run in parallel as read-only. Empty = default (`fetch`, `qdrant-find`, filesystem reads, `grep`); setting it **replaces** the default. Mutating MCP tools (shell, writes, `qdrant-store`) always run serially. |
| `AGENT_PERMISSIONS` | bool | `true` | Permission policy engine on. `false` = NullPolicy (everything allowed, for baselining). |
| `AGENT_PERMISSION_ALLOW` / `_DENY` / `_ASK` | str (csv) | _(empty)_ | Rules, each `Tool` (whole tool) or `Tool(content)` (argument-scoped). deny > ask > allow. Content can't contain a comma via env. |
| `AGENT_PERMISSION_DEFAULT` | str | `allow` | Behavior when no rule matches (`allow`/`deny`/`ask`). |
| `AGENT_PERMISSION_ASK_RESOLUTION` | str | `deny` | How an `ask` resolves on the headless server (`deny`/`allow`); the call is still recorded as an `ask`. |

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
| `AGENT_GATEWAY_CONFIG_PATH` | str | `agentgateway/config.yaml` | The gateway config you own and edit. |
| `AGENT_GATEWAY_EFFECTIVE_CONFIG_PATH` | str | `.redcell/agentgateway.yaml` | Rendered copy `serve` actually passes as `-f`: targets that cannot start are dropped and `QDRANT_URL` is filled in from `AGENT_QDRANT_HOST`/`_PORT`. Generated at every start; do not edit. |
| `AGENT_GATEWAY_HOST` | str | `127.0.0.1` | Host for the readiness probe. |
| `AGENT_GATEWAY_PORT` | int | `3030` | Port for the readiness probe. |
| `AGENT_GATEWAY_URL` | str | `http://127.0.0.1:3030/mcp` | The aggregated MCP endpoint the agent connects to. Tune the path (root vs `/mcp`) to match your gateway config. |
| `AGENT_GATEWAY_AUTOSTART` | bool | `true` | If false, `serve` does not spawn the gateway (run it yourself elsewhere). |
| `AGENT_GATEWAY_READY_TIMEOUT` | float | `30.0` | Seconds to wait for the gateway port to accept connections before continuing without it. |
| `AGENT_OPENSHELL_AUTOSTART` | bool | `true` | Create/reuse the OpenShell execution sandbox at `serve` startup and write the SSH config the gateway launches `shell`/`filesystem` through. False leaves both tools erroring. |
| `AGENT_OPENSHELL_BIN` | str | `openshell` | The OpenShell CLI executable. |
| `AGENT_OPENSHELL_GATEWAY_URL` | str | `http://127.0.0.1:8080` | OpenShell control-plane URL. Must be published on port 8080 specifically — the docker driver tells sandboxes to call back on the gateway's own port, so remapping it hangs them in `Provisioning`. |
| `AGENT_OPENSHELL_HEALTH_URL` | str | `http://127.0.0.1:8081/healthz` | Readiness probe. A separate port from the control plane. |
| `AGENT_OPENSHELL_GATEWAY_NAME` | str | `redcell` | Local name the gateway is registered under. |
| `AGENT_OPENSHELL_SANDBOX` | str | `redcell-sbx` | Sandbox name to create or reuse. |
| `AGENT_OPENSHELL_WORKSPACE` | str | `default` | OpenShell workspace. Part of the generated SSH host alias. |
| `AGENT_OPENSHELL_IMAGE` | str | `redcell-sandbox:local` | Sandbox image. The compose stack builds it (`sandbox-image` service); in host mode run `docker build -t redcell-sandbox:local sandbox/`. |
| `AGENT_OPENSHELL_POLICY_PATH` | str | `sandbox/policy.yaml` | Policy applied at sandbox creation. |
| `AGENT_OPENSHELL_SSH_CONFIG_PATH` | str | `.redcell/openshell_ssh_config` | Where the generated SSH config is written; `agentgateway/config.yaml` points `ssh -F` at it. |
| `AGENT_OPENSHELL_READY_TIMEOUT` | float | `60.0` | Seconds to wait for the gateway health endpoint. |
| `AGENT_OPENSHELL_CREATE_TIMEOUT` | float | `600.0` | Seconds allowed for sandbox creation. Generous because a cold first run pulls images. |
| `AGENT_OPENSHELL_DELETE_ON_EXIT` | bool | `false` | Delete the sandbox on shutdown. Reusing it skips image pulls and keeps `/sandbox`. |

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
| `AGENT_QDRANT_MANAGE` | bool | `true` | If false, `serve` never shells out to `docker compose` — it only waits for the port. Used by the containerized `redcell` service, which has no Docker socket and relies on Compose itself to start and health-gate Qdrant. |
| `AGENT_QDRANT_COMPOSE_FILE` | str | `docker-compose.yml` | Compose file passed as `-f`. Ignored when `AGENT_QDRANT_MANAGE=false`. |
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

## OpenTelemetry tracing (opt-in)

Off by default — no SDK initialization, no overhead. Full detail, including the
span tree shape and the content-capture warning, is in
[observability.md](observability.md).

| Env var | Type | Default | Meaning |
| ------- | ---- | ------- | ------- |
| `AGENT_TRACING` | bool | `false` | Master switch. Needs the `tracing` extra (`uv sync --extra tracing`); the Docker image installs it unconditionally. |
| `AGENT_TRACING_ENDPOINT` | str | `http://127.0.0.1:4317` | OTLP collector endpoint. `http://otel-collector:4317` inside the compose stack. |
| `AGENT_TRACING_PROTOCOL` | str | `grpc` | `grpc` or `http`. |
| `AGENT_TRACING_SERVICE_NAME` | str | `redcell` | `service.name` resource attribute on every span. |
| `AGENT_TRACING_SAMPLE_RATIO` | float | `1.0` | Trace-id ratio sampler. Keep at `1.0` for scans — every run matters. |
| `AGENT_TRACING_INSTRUMENT_HTTP` | bool | `true` | FastAPI + httpx auto-instrumentation (request root span + outbound client spans to the model, SearXNG, and AgentGateway). |

## Container overrides (`docker-compose.yml`)

The `redcell` service reads `.env` via `env_file`, then its own `environment:` block
wins over it on purpose — `.env` holds host-oriented values (`127.0.0.1`) that would
point at the wrong place from inside a container. Compose always overrides:
`AGENT_SEARXNG_URL` → `http://searxng:8080`, `AGENT_QDRANT_HOST` → `qdrant`,
`AGENT_QDRANT_MANAGE` → `false`, `AGENT_OPENSHELL_GATEWAY_URL` →
`http://openshell-gateway:8080`, `AGENT_OPENSHELL_HEALTH_URL` →
`http://openshell-gateway:8081/healthz`. Setting these in `.env` has no effect in
container mode; edit `docker-compose.yml` instead. `AGENT_GATEWAY_URL` stays on
loopback (`http://127.0.0.1:3030/mcp`) either way, since AgentGateway still runs as
a child process inside the same container.

## Full `.env.example`

The repository's [`.env.example`](../.env.example) contains every variable above with
inline comments — copy it to `.env` and edit. Nothing in it is required to start a
chat against a hosted model except `AGENT_MODEL` and the matching provider key.

## Programmatic configuration

When embedding redcell as a library, construct `Settings()` (it still reads env/`.env`)
or pass values directly to the components — `Agent`, `LLM`, `create_app`, `SessionStore`,
`make_guardrail`, `build_system_prompt` all take plain arguments and do not require the
`Settings` object. See [development.md](development.md).
