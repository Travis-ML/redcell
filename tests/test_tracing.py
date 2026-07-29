"""Tests for OTel tracing: the span tree shape, not the exporter.

Uses an in-memory exporter so nothing leaves the process and assertions are on
real finished spans rather than mocks.
"""

import pytest

from redcell.observability import Hooks
from redcell.tracing import setup_tracing

pytest.importorskip("opentelemetry.sdk", reason="needs the `tracing` extra")

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from redcell.tracing import TracingHooks  # noqa: E402


@pytest.fixture
def traced():
    """A Hooks wired to TracingHooks, plus the exporter collecting its spans."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    hooks = Hooks()
    TracingHooks(tracer=provider.get_tracer("test")).attach(hooks)
    return hooks, exporter


def _by_name(exporter):
    return {s.name: s for s in exporter.get_finished_spans()}


def test_disabled_by_default_is_a_noop():
    class S:
        tracing = False

    assert setup_tracing(S()) is False


def test_full_run_produces_a_span_tree(traced):
    hooks, exporter = traced
    hooks.emit("run_start", run_id="r1")
    hooks.emit("llm_start", run_id="r1", model="hosted_vllm/gemma")
    hooks.emit(
        "llm_end",
        run_id="r1",
        model="hosted_vllm/gemma",
        usage={"prompt_tokens": 11, "completion_tokens": 5},
    )
    hooks.emit(
        "tool_start", run_id="r1", name="shell_run_process", args={"command_line": "id"}, id="c1"
    )
    hooks.emit("tool_end", run_id="r1", name="shell_run_process", id="c1", result="uid=1000")
    hooks.emit("run_end", run_id="r1")

    spans = _by_name(exporter)
    assert set(spans) == {"agent.run", "chat hosted_vllm/gemma", "execute_tool shell_run_process"}

    run = spans["agent.run"]
    llm = spans["chat hosted_vllm/gemma"]
    tool = spans["execute_tool shell_run_process"]

    # Both children hang off the run, so a backend renders one tree per request.
    assert llm.parent.span_id == run.context.span_id
    assert tool.parent.span_id == run.context.span_id
    assert llm.context.trace_id == run.context.trace_id == tool.context.trace_id

    assert llm.attributes["gen_ai.request.model"] == "hosted_vllm/gemma"
    assert llm.attributes["gen_ai.system"] == "hosted_vllm"
    assert llm.attributes["gen_ai.operation.name"] == "chat"
    assert llm.attributes["gen_ai.usage.input_tokens"] == 11
    assert llm.attributes["gen_ai.usage.output_tokens"] == 5

    assert tool.attributes["gen_ai.tool.name"] == "shell_run_process"
    assert tool.attributes["gen_ai.tool.call.id"] == "c1"


def test_tool_content_is_captured(traced):
    # Content capture is the point for red-team forensics: a span saying "one tool
    # call, 1.2s" cannot reconstruct what the agent actually did.
    hooks, exporter = traced
    hooks.emit("run_start", run_id="r1")
    hooks.emit(
        "tool_start", run_id="r1", name="shell", args={"command_line": "curl http://evil"}, id="c1"
    )
    hooks.emit("tool_end", run_id="r1", name="shell", id="c1", result="pwned")
    hooks.emit("run_end", run_id="r1")

    tool = _by_name(exporter)["execute_tool shell"]
    assert "curl http://evil" in tool.attributes["redcell.tool.arguments"]
    assert "pwned" in tool.attributes["redcell.tool.result"]


def test_tool_error_marks_span_failed(traced):
    hooks, exporter = traced
    hooks.emit("run_start", run_id="r1")
    hooks.emit("tool_start", run_id="r1", name="shell", args={}, id="c1")
    hooks.emit("tool_end", run_id="r1", name="shell", id="c1", result="boom", is_error=True)
    hooks.emit("run_end", run_id="r1")

    tool = _by_name(exporter)["execute_tool shell"]
    assert tool.attributes["redcell.tool.is_error"] is True
    assert tool.status.is_ok is False


def test_guardrail_and_permission_land_as_run_events(traced):
    hooks, exporter = traced
    hooks.emit("run_start", run_id="r1")
    hooks.emit("guardrail_input_block", run_id="r1", reason="jailbreak", categories=["harm"])
    hooks.emit("permission", run_id="r1", tool="shell", decision="deny")
    hooks.emit("compaction", run_id="r1", before=9000, after=4000)
    hooks.emit("run_end", run_id="r1")

    run = _by_name(exporter)["agent.run"]
    names = [e.name for e in run.events]
    assert names == ["guardrail_input_block", "permission", "compaction"]
    assert run.events[0].attributes["reason"] == "jailbreak"


def test_max_iterations_marks_the_run_failed(traced):
    hooks, exporter = traced
    hooks.emit("run_start", run_id="r1")
    hooks.emit("max_iterations", run_id="r1", limit=25)
    hooks.emit("run_end", run_id="r1")

    run = _by_name(exporter)["agent.run"]
    assert run.status.is_ok is False


def test_open_children_are_closed_on_run_end(traced):
    # agent.run has three exit paths, two of which are error paths, so a run can
    # end with an LLM or tool span still open. Leaving them unended loses the
    # whole subtree — exactly the trace you most want when something went wrong.
    hooks, exporter = traced
    hooks.emit("run_start", run_id="r1")
    hooks.emit("llm_start", run_id="r1", model="m")
    hooks.emit("tool_start", run_id="r1", name="shell", args={}, id="c1")
    hooks.emit("run_end", run_id="r1")  # no llm_end, no tool_end

    spans = _by_name(exporter)
    assert set(spans) == {"agent.run", "chat m", "execute_tool shell"}
    assert spans["chat m"].attributes["redcell.incomplete"] is True
    assert spans["execute_tool shell"].attributes["redcell.incomplete"] is True


def test_concurrent_runs_do_not_cross_traces(traced):
    # A scan interleaves many requests; each run_id must get its own trace or
    # every session's spans end up in one unreadable tree.
    hooks, exporter = traced
    hooks.emit("run_start", run_id="a")
    hooks.emit("run_start", run_id="b")
    hooks.emit("tool_start", run_id="a", name="ta", args={}, id="1")
    hooks.emit("tool_start", run_id="b", name="tb", args={}, id="1")  # same call id!
    hooks.emit("tool_end", run_id="a", name="ta", id="1", result="ra")
    hooks.emit("tool_end", run_id="b", name="tb", id="1", result="rb")
    hooks.emit("run_end", run_id="a")
    hooks.emit("run_end", run_id="b")

    spans = _by_name(exporter)
    assert spans["execute_tool ta"].attributes["redcell.tool.result"] == "ra"
    assert spans["execute_tool tb"].attributes["redcell.tool.result"] == "rb"
    assert spans["execute_tool ta"].context.trace_id != spans["execute_tool tb"].context.trace_id


def test_state_is_dropped_after_run_end(traced):
    # Per-run dicts must not grow across a long scan.
    hooks, _exporter = traced
    tracing = None
    for cb in hooks._callbacks["run_start"]:
        tracing = cb.__self__
    for i in range(50):
        hooks.emit("run_start", run_id=f"r{i}")
        hooks.emit("tool_start", run_id=f"r{i}", name="t", args={}, id="c")
        hooks.emit("tool_end", run_id=f"r{i}", name="t", id="c", result="x")
        hooks.emit("run_end", run_id=f"r{i}")
    assert tracing._runs == {}
    assert tracing._tools == {}
    assert tracing._llm == {}


def test_events_without_a_run_are_ignored(traced):
    # Hooks are fire-and-forget; a stray event must not raise or invent a span.
    hooks, exporter = traced
    hooks.emit("llm_end", run_id="nope", usage=None)
    hooks.emit("tool_end", run_id="nope", name="t", id="c", result="x")
    hooks.emit("permission", run_id="nope", decision="deny")
    hooks.emit("run_end", run_id="nope")
    assert exporter.get_finished_spans() == ()


def test_large_attributes_are_truncated(traced):
    hooks, exporter = traced
    hooks.emit("run_start", run_id="r1")
    hooks.emit("tool_start", run_id="r1", name="t", args={"blob": "x" * 100_000}, id="c")
    hooks.emit("tool_end", run_id="r1", name="t", id="c", result="ok")
    hooks.emit("run_end", run_id="r1")

    args = _by_name(exporter)["execute_tool t"].attributes["redcell.tool.arguments"]
    assert len(args) < 100_000
    assert "truncated" in args
