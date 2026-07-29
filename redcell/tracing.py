"""OpenTelemetry tracing for agent runs — opt-in, off by default.

Answers "what exactly did this agent do?" with a span tree per request: the HTTP
request at the root, the agent run beneath it, then a child span per LLM call and
per tool call, with guardrail trips, permission decisions and compaction attached
as span events.

It hangs off the existing :class:`~redcell.observability.Hooks` the same way
:class:`~redcell.accounting.CostAccountant` does, so the agent core needs no
knowledge of tracing beyond emitting ``run_start``.

Two deliberate choices:

**Content is always captured.** Prompts, completions and tool arguments go on the
spans. For a red-team tool the payload *is* the evidence — a trace saying "one
tool call, 1.2s" is useless for reconstructing an attack. The consequence, which
:doc:`docs/observability` states plainly, is that traces hold a *less* filtered
record than the API response does: the output guardrail redacts what reaches the
client, not what reached the span.

**OTel is an optional dependency**, imported inside :func:`setup_tracing` rather
than at module scope, so `uv sync` without the ``tracing`` extra still works and
importing :mod:`redcell` never pulls the SDK.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("redcell.tracing")

# GenAI semantic-convention attribute names, so a trace backend's LLM views work
# without per-app mapping. Kept in one place because the conventions are still
# moving between spec revisions.
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_OPERATION = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_USAGE_INPUT = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT = "gen_ai.usage.output_tokens"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"

_MAX_ATTR_CHARS = 32_768


def setup_tracing(settings: Any) -> bool:
    """Install a tracer provider and the HTTP instrumentors. Returns success.

    A no-op returning False when tracing is disabled, so callers can guard
    unconditionally. Never raises: telemetry must not be able to stop the server
    from starting.
    """
    if not getattr(settings, "tracing", False):
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
    except ImportError:
        logger.warning(
            "AGENT_TRACING is on but the OpenTelemetry SDK is missing; "
            "install it with `uv sync --extra tracing`. Continuing untraced."
        )
        return False

    try:
        exporter = _make_exporter(settings)
        provider = TracerProvider(
            resource=Resource.create({"service.name": settings.tracing_service_name}),
            sampler=TraceIdRatioBased(settings.tracing_sample_ratio),
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    except Exception as exc:
        logger.warning("could not initialize tracing (%s); continuing untraced", exc)
        return False

    if getattr(settings, "tracing_instrument_http", True):
        _instrument_http()
    logger.info(
        "tracing enabled: exporting to %s as service %r",
        settings.tracing_endpoint,
        settings.tracing_service_name,
    )
    return True


def _make_exporter(settings: Any):
    """Build the OTLP span exporter for the configured protocol."""
    if settings.tracing_protocol == "http":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    else:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    return OTLPSpanExporter(endpoint=settings.tracing_endpoint)


def _instrument_http() -> None:
    """Auto-instrument FastAPI and httpx, tolerating either being absent.

    FastAPI gives a server span per request, which becomes the trace root and
    parents the agent run. httpx covers everything redcell calls out to — the
    model endpoint via LiteLLM, SearXNG for web_search, and the MCP session to
    AgentGateway — and injects W3C traceparent, so AgentGateway's own spans can
    join the same trace.
    """
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception as exc:
        logger.debug("httpx instrumentation unavailable: %s", exc)


def instrument_app(app: Any) -> None:
    """Attach the FastAPI instrumentor to ``app``. Safe to call when unavailable."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception as exc:
        logger.debug("FastAPI instrumentation unavailable: %s", exc)


def current_trace_ids() -> tuple[str, str] | None:
    """``(trace_id, span_id)`` as hex for the active span, or None if untraced.

    Used by the structlog processor so a log line can be joined to its trace.
    Returns None rather than zeros when there is no recording span, so the fields
    are simply absent instead of misleadingly present.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return None
    return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"


def _stringify(value: Any) -> str:
    """Render an event payload for a span attribute, bounded in size.

    Span attributes must be primitives, and an exporter will drop or truncate a
    pathologically large one, so bound it here where the truncation is visible
    rather than silent.
    """
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=repr)
        except Exception:
            text = repr(value)
    if len(text) > _MAX_ATTR_CHARS:
        return text[:_MAX_ATTR_CHARS] + f"…[truncated, {len(text)} chars total]"
    return text


class TracingHooks:
    """Turn agent lifecycle events into a span tree.

    Attach with :meth:`attach`, mirroring ``CostAccountant``. Spans are held in
    dicts keyed by ``run_id`` (and ``call_id`` for tools) because the hook API is
    fire-and-forget and carries no span context; every entry for a run is dropped
    on ``run_end`` so memory stays bounded across a long scan.
    """

    def __init__(self, tracer: Any = None) -> None:
        if tracer is None:
            from opentelemetry import trace

            tracer = trace.get_tracer("redcell")
        self._tracer = tracer
        self._runs: dict[str, Any] = {}
        self._llm: dict[str, Any] = {}
        self._tools: dict[tuple[str, str], Any] = {}

    def attach(self, hooks: Any) -> TracingHooks:
        hooks.on("run_start", self._run_start)
        hooks.on("run_end", self._run_end)
        hooks.on("llm_start", self._llm_start)
        hooks.on("llm_end", self._llm_end)
        hooks.on("tool_start", self._tool_start)
        hooks.on("tool_end", self._tool_end)
        for event in (
            "guardrail_input_block",
            "guardrail_output_redact",
            "guardrail_tool_redact",
            "permission",
            "compaction",
            "max_iterations",
        ):
            hooks.on(event, self._annotate(event))
        return self

    # --- run ---------------------------------------------------------------
    def _run_start(self, run_id: str = "", **payload: Any) -> None:
        # start_span, not start_as_current_span: the run outlives this callback,
        # so it cannot be scoped to a `with` block. It still parents correctly
        # because the FastAPI server span is current when the run begins.
        span = self._tracer.start_span("agent.run")
        span.set_attribute("redcell.run_id", run_id)
        for key, value in payload.items():
            span.set_attribute(f"redcell.{key}", _stringify(value))
        self._runs[run_id] = span

    def _run_end(self, run_id: str = "", **_payload: Any) -> None:
        # Close children first: an LLM or tool span still open here means the run
        # was cut short (an exception, or max_iterations), and leaving them open
        # loses the whole subtree.
        llm = self._llm.pop(run_id, None)
        if llm is not None:
            llm.set_attribute("redcell.incomplete", True)
            llm.end()
        for key in [k for k in self._tools if k[0] == run_id]:
            span = self._tools.pop(key)
            span.set_attribute("redcell.incomplete", True)
            span.end()
        run = self._runs.pop(run_id, None)
        if run is not None:
            run.end()

    # --- llm ---------------------------------------------------------------
    def _llm_start(self, run_id: str = "", model: str = "", **payload: Any) -> None:
        parent = self._runs.get(run_id)
        span = self._tracer.start_span(f"chat {model}", context=_ctx(parent))
        span.set_attribute(GEN_AI_OPERATION, "chat")
        span.set_attribute(GEN_AI_REQUEST_MODEL, model)
        span.set_attribute(GEN_AI_SYSTEM, _provider_of(model))
        for key, value in payload.items():
            span.set_attribute(f"redcell.{key}", _stringify(value))
        self._llm[run_id] = span

    def _llm_end(self, run_id: str = "", usage: Any = None, **payload: Any) -> None:
        span = self._llm.pop(run_id, None)
        if span is None:
            return
        for attr, key in ((GEN_AI_USAGE_INPUT, "input"), (GEN_AI_USAGE_OUTPUT, "output")):
            value = _usage_get(usage, key)
            if value is not None:
                span.set_attribute(attr, value)
        for key, value in payload.items():
            if key != "model":
                span.set_attribute(f"redcell.{key}", _stringify(value))
        span.end()

    # --- tools -------------------------------------------------------------
    def _tool_start(
        self, run_id: str = "", name: str = "", args: Any = None, id: str = "", **_p: Any
    ) -> None:
        parent = self._runs.get(run_id)
        span = self._tracer.start_span(f"execute_tool {name}", context=_ctx(parent))
        span.set_attribute(GEN_AI_TOOL_NAME, name)
        if id:
            span.set_attribute(GEN_AI_TOOL_CALL_ID, id)
        if args is not None:
            span.set_attribute("redcell.tool.arguments", _stringify(args))
        self._tools[(run_id, id)] = span

    def _tool_end(
        self,
        run_id: str = "",
        name: str = "",
        id: str = "",
        result: Any = None,
        is_error: bool = False,
        **payload: Any,
    ) -> None:
        span = self._tools.pop((run_id, id), None)
        if span is None:
            return
        if result is not None:
            span.set_attribute("redcell.tool.result", _stringify(result))
        span.set_attribute("redcell.tool.is_error", bool(is_error))
        if is_error:
            _set_error(span, f"tool {name} returned an error")
        for key, value in payload.items():
            span.set_attribute(f"redcell.{key}", _stringify(value))
        span.end()

    # --- everything else ---------------------------------------------------
    def _annotate(self, event: str):
        """Record a point-in-time event on the run span.

        Guardrail trips, permission decisions, compaction and max_iterations have
        no duration, so they are span events rather than spans. Attaching them to
        the run keeps them visible even when nothing else is open.
        """

        def handler(run_id: str = "", **payload: Any) -> None:
            span = self._runs.get(run_id)
            if span is None:
                return
            attrs = {k: _stringify(v) for k, v in payload.items()}
            span.add_event(event, attributes=attrs)
            if event == "max_iterations":
                _set_error(span, "hit max_iterations")

        return handler


def _ctx(parent: Any):
    """A context with ``parent`` as the current span, or None for no parent."""
    if parent is None:
        return None
    from opentelemetry import trace

    return trace.set_span_in_context(parent)


def _set_error(span: Any, message: str) -> None:
    """Mark a span as failed without importing StatusCode at module scope."""
    try:
        from opentelemetry.trace import Status, StatusCode

        span.set_status(Status(StatusCode.ERROR, message))
    except Exception:
        span.set_attribute("redcell.error", message)


def _provider_of(model: str) -> str:
    """The provider half of a LiteLLM model id (``hosted_vllm/foo`` -> ``hosted_vllm``)."""
    return model.split("/", 1)[0] if "/" in model else model or "unknown"


def _usage_get(usage: Any, key: str) -> int | None:
    """Pull a normalized token count out of whatever shape ``usage`` has.

    Reuses :func:`redcell.pricing.normalize_usage`, which already handles the
    provider-to-provider differences the accountant deals with.
    """
    if usage is None:
        return None
    try:
        from .pricing import normalize_usage

        return normalize_usage(usage).get(key)
    except Exception:
        return None
