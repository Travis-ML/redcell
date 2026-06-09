"""Tests for the safety-policy system prompt assembly."""

from redcell.prompts import (
    BASE_SYSTEM_PROMPT,
    SAFETY_POLICY,
    SAFETY_RULES,
    build_safety_policy,
    build_system_prompt,
)


def test_safety_on_prepends_policy():
    prompt = build_system_prompt(safety=True)
    assert SAFETY_POLICY in prompt
    assert prompt.endswith(BASE_SYSTEM_PROMPT)


def test_safety_off_is_bare_base():
    assert build_system_prompt(safety=False) == BASE_SYSTEM_PROMPT


def test_custom_base_preserved():
    prompt = build_system_prompt("You are redcell.", safety=True)
    assert prompt.endswith("You are redcell.")
    assert SAFETY_POLICY in prompt


def test_full_policy_includes_every_rule():
    for clause in SAFETY_RULES.values():
        assert clause in SAFETY_POLICY


def test_rule_subset_includes_only_selected():
    policy = build_safety_policy(["disclosure"])
    assert SAFETY_RULES["disclosure"] in policy
    assert SAFETY_RULES["harm"] not in policy  # other rules excluded


def test_unknown_rule_names_are_ignored():
    # Only the valid name contributes; the bogus one is silently dropped.
    policy = build_safety_policy(["fairness", "does-not-exist"])
    assert SAFETY_RULES["fairness"] in policy


def test_build_system_prompt_forwards_rule_selection():
    prompt = build_system_prompt(safety=True, rules=["copyright"])
    assert SAFETY_RULES["copyright"] in prompt
    assert SAFETY_RULES["truthfulness"] not in prompt
