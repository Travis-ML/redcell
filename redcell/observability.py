"""Event hooks and structured logging for agent runs."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable

import structlog

EventName = str  # "llm_start" | "llm_end" | "tool_start" | "tool_end"


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog + stdlib logging once at startup."""
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO))
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )


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
    for event in ("llm_start", "llm_end", "tool_start", "tool_end"):
        hooks.on(event, lambda _event=event, **kw: log.info(_event, **kw))
    return hooks
