"""Event hooks and structured logging for agent runs."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable

import structlog

EventName = str  # see LIFECYCLE_EVENTS for the full set

# Every event the agent emits. logging_hooks subscribes to all of them so the
# guardrail and max_iterations events surface, not just the LLM/tool lifecycle.
LIFECYCLE_EVENTS = (
    "run_start",
    "llm_start",
    "llm_end",
    "tool_start",
    "tool_end",
    "max_iterations",
    "run_end",
    "permission",
    "compaction",
    "guardrail_input_block",
    "guardrail_output_redact",
    "guardrail_tool_redact",
)


# The MCP streamable-HTTP transport logs benign teardown-race noise on every
# short-lived session close: an "Error parsing SSE message" (actually a
# ClosedResourceError as the GET listener stream is torn down), a moot reconnect,
# and "Session termination failed: 202" (AgentGateway returns 202 Accepted for the
# DELETE, which the SDK doesn't whitelist alongside 200/204). None affect tool
# calls — they succeed over the POST stream — so we quiet this logger by default.
_NOISY_MCP_LOGGER = "mcp.client.streamable_http"


def configure_logging(
    level: str = "INFO",
    *,
    json_logs: bool = False,
    log_file: str | None = None,
    quiet_mcp_transport: bool = True,
) -> None:
    """Configure structlog + stdlib logging once at startup.

    Args:
        level: minimum log level.
        json_logs: render events as JSON instead of the human console format.
            Combined with ``log_file`` this yields a JSONL event sink a scanner
            run can be analyzed from afterwards.
        log_file: if set, write logs to this file (append) instead of stderr.
        quiet_mcp_transport: silence the MCP streamable-HTTP transport's benign
            teardown-race logs (see :data:`_NOISY_MCP_LOGGER`). Set False to keep
            them when debugging the transport itself.
    """
    num_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=num_level, force=True)
    # Raise the transport logger above ERROR so its teardown noise is suppressed
    # (it logs the SSE race at ERROR); restore to the chosen level when not quiet.
    logging.getLogger(_NOISY_MCP_LOGGER).setLevel(
        logging.CRITICAL if quiet_mcp_transport else num_level
    )
    renderer = structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()

    def add_trace_ids(_logger, _name, event_dict):
        """Tag each line with the active trace/span id, when there is one.

        This is what lets a log backend jump from a line to its trace and back.
        The fields are omitted rather than zeroed when nothing is being traced, so
        a filter on trace_id never matches untraced lines.
        """
        ids = _current_trace_ids()
        if ids is not None:
            event_dict["trace_id"], event_dict["span_id"] = ids
        return event_dict

    # structlog renders via its own PrintLogger (not stdlib handlers); point it at
    # the file directly so a JSON run lands in a JSONL sink rather than stdout.
    sink = open(log_file, "a", encoding="utf-8") if log_file else None  # noqa: SIM115
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(num_level),
        logger_factory=structlog.PrintLoggerFactory(file=sink),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            add_trace_ids,
            renderer,
        ],
    )


def _current_trace_ids() -> tuple[str, str] | None:
    """Active ``(trace_id, span_id)``, or None when tracing is off or absent.

    Imported lazily and failure-tolerant: logging must keep working with the OTel
    extra uninstalled, which is the default.
    """
    try:
        from .tracing import current_trace_ids

        return current_trace_ids()
    except Exception:
        return None


class Hooks:
    """Registry of callbacks fired on agent lifecycle events.

    Register with :meth:`on` and fire with :meth:`emit`. Callbacks receive the
    event payload as keyword arguments. Unknown events and missing callbacks
    are silently ignored, so hooks are always safe to emit.
    """

    def __init__(self) -> None:
        self._callbacks: dict[EventName, list[Callable[..., None]]] = defaultdict(list)

    def on(self, event: EventName, callback: Callable[..., None]) -> None:
        self._callbacks[event].append(callback)

    def emit(self, event: EventName, **payload: object) -> None:
        for cb in self._callbacks.get(event, ()):
            cb(**payload)


def logging_hooks(logger: structlog.BoundLogger | None = None) -> Hooks:
    """Return :class:`Hooks` preconfigured to log every lifecycle event."""
    log = logger or structlog.get_logger("redcell")
    hooks = Hooks()
    for event in LIFECYCLE_EVENTS:
        hooks.on(event, lambda _event=event, **kw: log.info(_event, **kw))
    return hooks
