# Observability

Two layers, independent of each other:

- **Structured logs** — always on. structlog events for every lifecycle step, with
  a per-run `scorecard` (tokens, USD, tool and guardrail counts). JSON + a file
  gives you a JSONL sink you can analyze after a scan.
- **OpenTelemetry traces** — opt-in. One trace per request, so you can reconstruct
  exactly what an agent did and in what order.

## What a trace contains

```
POST /v1/chat/completions              (FastAPI server span — the root)
└── agent.run                          redcell.run_id, redcell.session_id
    ├── chat hosted_vllm/gemma-4-26B   gen_ai.request.model, usage.input/output_tokens
    ├── execute_tool web_search        gen_ai.tool.name, arguments, result
    │   └── GET searxng/search         (httpx client span)
    ├── execute_tool shell_run_process arguments = the actual command line
    ├── chat hosted_vllm/gemma-4-26B
    └── [span events] guardrail_input_block, permission, compaction, max_iterations
```

The mapping lives in `redcell/tracing.py`. It attaches to the same `Hooks` object
`CostAccountant` uses, so the agent core knows nothing about tracing beyond
emitting `run_start`.

Attribute names follow the OpenTelemetry GenAI semantic conventions
(`gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.tool.name`), so a
backend's built-in LLM views work without per-app mapping.

Three sources feed one trace. The first two are automatic; the third is a
commented-out block in `agentgateway/config.yaml` you uncomment, because that file
has no env interpolation and the collector address differs between host mode and
the compose stack.

| Source | Gives you |
|--------|-----------|
| `redcell/tracing.py` via `Hooks` | the run, LLM calls, tool calls, guardrail/permission events |
| FastAPI + httpx auto-instrumentation | the request root, and client spans to the model, SearXNG, and AgentGateway |
| AgentGateway's own OTLP export (opt-in) | a proxy span per MCP tool call, separating agent-side from gateway-side latency |

## Traces include prompt and tool content

Spans carry full prompts, completions, and tool arguments. For a red-team tool
that is the point — a span reading "one tool call, 1.2s" cannot reconstruct an
attack.

The consequence is worth stating plainly: **a trace backend holds a less filtered
record than the API response does.** The output guardrail redacts what reaches the
client, not what reached the span, so content the client never saw is still in the
trace. Prefer the local Grafana profile over a cloud backend for sensitive runs.

Attributes are truncated at 32KB with an explicit `…[truncated, N chars total]`
marker, so a huge payload is visibly clipped rather than silently dropped by an
exporter.

## Enabling it

Tracing needs an optional dependency group:

```bash
uv sync --extra tracing
AGENT_TRACING=true uv run redcell serve
```

The Docker image installs the extra unconditionally, so in the compose stack it is
one flag.

### Local Grafana stack

Everything stays on your machine — no account, no egress:

```bash
AGENT_TRACING=true docker compose --profile observability up -d
open http://localhost:3001          # Grafana; 3000 is taken by Open WebUI
```

That starts an OTel Collector (4317 gRPC / 4318 HTTP), Tempo for traces, Loki for
logs, and Grafana with both datasources pre-wired. With no backend running at all
the collector's `debug` exporter still prints spans, so
`docker compose logs otel-collector` is a useful smoke test.

### Grafana Cloud

Put these in `.env`:

```bash
GRAFANA_CLOUD_OTLP_ENDPOINT=https://otlp-gateway-<region>.grafana.net/otlp
GRAFANA_CLOUD_OTLP_AUTH=<base64 of "instanceID:token">    # printf '%s' 'id:token' | base64
```

then select the other collector config:

```bash
OTEL_CONFIG=./otel/collector-grafanacloud.yaml \
  docker compose --profile observability up -d
```

One endpoint takes traces, logs, and metrics; the signal path is appended
automatically.

There are two collector config files rather than one with conditional exporters
because the Collector cannot disable an exporter at runtime — an exporter with no
backend behind it retries and logs on every batch, forever.

## Jumping between a log line and its trace

`configure_logging` adds a structlog processor that stamps every line with the
active `trace_id` and `span_id`. The fields are *omitted* rather than zeroed when
nothing is being traced, so a filter on `trace_id` never matches untraced lines.

Grafana's provisioned datasources use that field in both directions: a span links
to its Loki lines, and a log line's `trace_id` links back into Tempo.

Log shipping reuses the JSONL sink that already exists rather than adding a second
logging path in Python — the collector's `file_log` receiver tails it:

```bash
AGENT_LOG_JSON=true AGENT_LOG_FILE=/var/log/redcell/events.jsonl
```

The compose stack mounts that path as a shared volume, always, so turning tracing
on later needs no compose change.

## Settings

| Variable | Default | Notes |
|----------|---------|-------|
| `AGENT_TRACING` | `false` | Master switch. Off means no SDK initialization and no overhead. |
| `AGENT_TRACING_ENDPOINT` | `http://127.0.0.1:4317` | OTLP collector endpoint. `http://otel-collector:4317` in compose. |
| `AGENT_TRACING_PROTOCOL` | `grpc` | `grpc` or `http`. |
| `AGENT_TRACING_SERVICE_NAME` | `redcell` | `service.name` on every span. |
| `AGENT_TRACING_SAMPLE_RATIO` | `1.0` | Trace-id ratio sampler. Keep at 1.0 for scans; every run matters. |
| `AGENT_TRACING_INSTRUMENT_HTTP` | `true` | FastAPI + httpx auto-instrumentation. |

## Degradation

Telemetry must never be why the server won't start, so every failure path is a
warning and a fallback to untraced:

- `AGENT_TRACING=true` without the extra installed → one warning naming the fix.
- A collector that is down → the SDK's exporter retries and drops in the
  background; requests are unaffected.
- OTel missing entirely → the log processor's lazy import fails closed and the
  `trace_id` fields are simply absent.

## Cost and activity, without tracing

The per-run `scorecard` event already lands in the log stream with tokens, USD
cost, and LLM/tool/guardrail counts (`redcell/accounting.py`, priced by
`redcell/pricing.py`). No metrics pipeline is configured, because that event
covers the same ground and can be queried straight out of Loki.
