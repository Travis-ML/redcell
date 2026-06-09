"""Tests for the safety-policy system prompt assembly."""

from redcell.prompts import BASE_SYSTEM_PROMPT, SAFETY_POLICY, build_system_prompt


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
