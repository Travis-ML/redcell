"""Scan scorecard: aggregate cost/token/activity from agent events.

A :class:`CostAccountant` subscribes to the agent's :class:`~redcell.observability.Hooks`
and tallies, per ``run_id`` and across the whole process, the tokens, dollar cost,
and counts of LLM calls / tool calls / tool errors / guardrail trips. On each
``run_end`` it logs one ``scorecard`` event (so it lands in the JSONL sink for a
scan) and folds that run into a running grand total available via :meth:`totals`.

Per-run tallies are dropped on ``run_end`` so memory stays bounded over a long
scan (thousands of requests); the grand total accumulates as runs complete.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import structlog

from .observability import Hooks
from .pricing import cost_usd, normalize_usage

_GUARDRAIL_EVENTS = (
    "guardrail_input_block",
    "guardrail_output_redact",
    "guardrail_tool_redact",
)


@dataclass
class ModelUsage:
    """Accumulated token usage and cost for one model."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0
    calls: int = 0

    def add(self, usage: dict | None, cost: float) -> None:
        n = normalize_usage(usage)
        self.input += n["input"]
        self.output += n["output"]
        self.cache_read += n["cache_read"]
        self.cache_write += n["cache_write"]
        self.cost_usd += cost
        self.calls += 1

    def as_dict(self) -> dict:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "cost_usd": round(self.cost_usd, 6),
            "calls": self.calls,
        }


@dataclass
class RunTally:
    """All accounting for a single run (or the grand total across runs)."""

    by_model: dict[str, ModelUsage] = field(default_factory=lambda: defaultdict(ModelUsage))
    tool_calls: int = 0
    tool_errors: int = 0
    guardrail_trips: int = 0

    def total_cost(self) -> float:
        return sum(m.cost_usd for m in self.by_model.values())

    def total_tokens(self) -> int:
        return sum(m.input + m.output for m in self.by_model.values())

    def llm_calls(self) -> int:
        return sum(m.calls for m in self.by_model.values())

    def as_dict(self) -> dict:
        return {
            "cost_usd": round(self.total_cost(), 6),
            "total_tokens": self.total_tokens(),
            "llm_calls": self.llm_calls(),
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "guardrail_trips": self.guardrail_trips,
            "by_model": {name: mu.as_dict() for name, mu in self.by_model.items()},
        }


class CostAccountant:
    """Subscribes to agent hooks and tallies a per-run + grand-total scorecard."""

    def __init__(self, logger: structlog.BoundLogger | None = None) -> None:
        self._runs: dict[str, RunTally] = {}
        self.grand = RunTally()
        self.unknown_models: set[str] = set()
        self._log = logger or structlog.get_logger("redcell.scorecard")

    def attach(self, hooks: Hooks) -> None:
        """Register on the events that feed the scorecard."""
        hooks.on("llm_end", self.on_llm_end)
        hooks.on("tool_end", self.on_tool_end)
        for event in _GUARDRAIL_EVENTS:
            hooks.on(event, self.on_guardrail)
        hooks.on("run_end", self.on_run_end)

    def _run(self, run_id: str | None) -> RunTally:
        return self._runs.setdefault(run_id or "?", RunTally())

    def on_llm_end(
        self,
        run_id: str | None = None,
        model: str | None = None,
        usage: dict | None = None,
        **_: object,
    ) -> None:
        cost, unknown = cost_usd(model or "", usage)
        if unknown and model:
            self.unknown_models.add(model)
        name = model or "?"
        for tally in (self._run(run_id), self.grand):
            tally.by_model[name].add(usage, cost)

    def on_tool_end(self, run_id: str | None = None, is_error: bool = False, **_: object) -> None:
        for tally in (self._run(run_id), self.grand):
            tally.tool_calls += 1
            if is_error:
                tally.tool_errors += 1

    def on_guardrail(self, run_id: str | None = None, **_: object) -> None:
        for tally in (self._run(run_id), self.grand):
            tally.guardrail_trips += 1

    def on_run_end(self, run_id: str | None = None, **_: object) -> None:
        tally = self._runs.pop(run_id or "?", None)
        if tally is not None:
            self._log.info("scorecard", run_id=run_id, **tally.as_dict())

    def totals(self) -> RunTally:
        """The grand total across all completed runs so far."""
        return self.grand
