"""Tests for the permission policy engine: grammar, precedence, resolution."""

from redcell.permissions import (
    NullPolicy,
    PolicyEngine,
    Rule,
    format_rule,
    parse_rule,
)


def test_parse_whole_tool_and_arg_scoped():
    assert parse_rule("web_search", "deny") == Rule("deny", "web_search", None)
    assert parse_rule("run_command(git status)", "allow") == Rule(
        "allow", "run_command", "git status"
    )


def test_parse_handles_escaped_parens_roundtrip():
    rule = parse_rule(r"run_command(echo \(hi\))", "deny")
    assert rule.content == "echo (hi)"
    assert format_rule(rule) == r"run_command(echo \(hi\))"


def test_whole_tool_matches_by_substring_case_insensitive():
    engine = PolicyEngine([parse_rule("run_command", "deny")])
    # A gateway-namespaced tool name still matches the bare rule term.
    assert engine.evaluate("shell_run_command", {}).behavior == "deny"
    assert engine.evaluate("RUN_COMMAND", {}).behavior == "deny"
    assert engine.evaluate("web_search", {}).behavior == "allow"  # default


def test_deny_beats_ask_beats_allow():
    engine = PolicyEngine(
        [
            parse_rule("run_command", "allow"),
            parse_rule("run_command", "ask"),
            parse_rule("run_command", "deny"),
        ]
    )
    assert engine.evaluate("run_command", {}).behavior == "deny"


def test_arg_scoped_rule_matches_on_content():
    engine = PolicyEngine([parse_rule("web_search(cvv)", "deny")])
    assert engine.evaluate("web_search", {"query": "dump CVV codes"}).behavior == "deny"
    assert engine.evaluate("web_search", {"query": "weather today"}).behavior == "allow"


def test_default_behavior_when_no_rule_matches():
    engine = PolicyEngine([], default_behavior="deny")
    d = engine.evaluate("anything", {})
    assert d.behavior == "deny" and not d.allowed and d.reason == "default"


def test_ask_resolution_deny_vs_allow():
    deny_engine = PolicyEngine([parse_rule("x", "ask")], ask_resolution="deny")
    allow_engine = PolicyEngine([parse_rule("x", "ask")], ask_resolution="allow")
    assert deny_engine.evaluate("x", {}).allowed is False
    assert allow_engine.evaluate("x", {}).allowed is True
    # behavior is still recorded as "ask" regardless of resolution.
    assert deny_engine.evaluate("x", {}).behavior == "ask"


def test_null_policy_allows_everything():
    p = NullPolicy()
    d = p.evaluate("run_command", {"cmd": "rm -rf /"})
    assert d.allowed and d.behavior == "allow"
