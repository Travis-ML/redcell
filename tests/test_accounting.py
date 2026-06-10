"""Tests for the cost/token scorecard: pricing + the hook accountant."""

from redcell.accounting import CostAccountant
from redcell.observability import Hooks
from redcell.pricing import cost_usd, normalize_usage


def test_normalize_openai_usage_subtracts_cached_from_prompt():
    # OpenAI prompt_tokens INCLUDES cached; cached must be subtracted out.
    n = normalize_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": 300},
        }
    )
    assert n == {"input": 700, "output": 200, "cache_read": 300, "cache_write": 0}


def test_normalize_anthropic_usage_keeps_input_separate():
    n = normalize_usage(
        {
            "input_tokens": 500,
            "output_tokens": 100,
            "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 60,
        }
    )
    assert n == {"input": 500, "output": 100, "cache_read": 40, "cache_write": 60}


def test_cost_known_model():
    # claude-sonnet: $3/Mtok in, $15/Mtok out.
    cost, unknown = cost_usd(
        "anthropic/claude-sonnet-4-5", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    )
    assert not unknown
    assert round(cost, 2) == 18.0


def test_cost_unknown_model_is_zero_but_flagged():
    cost, unknown = cost_usd(
        "hosted_vllm/gemma-4", {"prompt_tokens": 1000, "completion_tokens": 500}
    )
    assert cost == 0.0
    assert unknown is True  # local model: tokens counted, no price


def _emit_run(hooks, run_id, model, usages, *, tool_calls=0, tool_errors=0, guardrails=0):
    for u in usages:
        hooks.emit("llm_end", run_id=run_id, model=model, usage=u)
    for i in range(tool_calls):
        hooks.emit("tool_end", run_id=run_id, is_error=i < tool_errors)
    for _ in range(guardrails):
        hooks.emit("guardrail_output_redact", run_id=run_id)
    hooks.emit("run_end", run_id=run_id)


def test_accountant_emits_scorecard_per_run_and_accumulates_grand_total():
    logged = []

    class _Log:
        def info(self, event, **kw):
            logged.append((event, kw))

    hooks = Hooks()
    acct = CostAccountant(logger=_Log())
    acct.attach(hooks)

    _emit_run(
        hooks,
        "run-1",
        "anthropic/claude-sonnet-4-5",
        [{"input_tokens": 1000, "output_tokens": 500}],
        tool_calls=3,
        tool_errors=1,
        guardrails=2,
    )

    assert len(logged) == 1
    event, card = logged[0]
    assert event == "scorecard"
    assert card["run_id"] == "run-1"
    assert card["llm_calls"] == 1
    assert card["tool_calls"] == 3
    assert card["tool_errors"] == 1
    assert card["guardrail_trips"] == 2
    assert card["total_tokens"] == 1500
    assert card["cost_usd"] > 0
    assert "anthropic/claude-sonnet-4-5" in card["by_model"]  # keyed by full model id
    # Per-run tally is dropped after run_end; grand total retains it.
    assert acct.totals().total_tokens() == 1500
    assert acct.totals().tool_calls == 3


def test_accountant_isolates_concurrent_runs():
    hooks = Hooks()
    acct = CostAccountant(logger=type("L", (), {"info": lambda *a, **k: None})())
    acct.attach(hooks)
    # Interleave two runs; counts must not cross-contaminate.
    hooks.emit("llm_end", run_id="A", model="m", usage={"input_tokens": 10, "output_tokens": 1})
    hooks.emit("llm_end", run_id="B", model="m", usage={"input_tokens": 20, "output_tokens": 2})
    hooks.emit("tool_end", run_id="A", is_error=False)
    hooks.emit("run_end", run_id="A")
    hooks.emit("run_end", run_id="B")
    assert acct.totals().llm_calls() == 2
    assert acct.totals().tool_calls == 1
