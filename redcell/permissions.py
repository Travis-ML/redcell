"""Permission policy engine: allow / deny / ask rules over tool calls.

A richer capability control than the all-or-nothing tool denylist. Rules are
three-valued (``allow``/``deny``/``ask``) and can scope a whole tool or a
specific argument; the agent consults the active :class:`Policy` before every
tool dispatch and emits a typed ``permission`` event for each gated call, so a
scan can measure *which* control blocked an attack (the same observability shape
as the guardrail).

Rule grammar (one per entry), mirroring the harness's ``Tool(content)`` form:

    web_search                 # whole-tool rule (matches the tool by name)
    run_command(git status)    # argument-scoped rule (matches call content)

``deny`` beats ``ask`` beats ``allow``; if nothing matches, the configured
default applies. Tool names are matched case-insensitively as a substring (so a
rule ``run_command`` also covers a gateway-namespaced ``shell_run_command``),
consistent with the denylist. ``ask`` has no human in the loop on a headless
server, so it resolves to a configured action (deny by default) while still
being recorded as an ``ask`` for measurement.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

Behavior = Literal["allow", "deny", "ask"]

# (tool_name, rule, arguments) -> whether the content rule matches the call. The
# whole rule is passed (not just content) so a matcher can vary by behavior — e.g.
# an allow rule must not match a compound shell command, while a deny rule does.
ContentMatcher = Callable[[str, "Rule", dict], bool]


@dataclass(frozen=True)
class Rule:
    """One permission rule. ``content`` None = whole-tool, else argument-scoped."""

    behavior: Behavior
    tool_name: str
    content: str | None = None


@dataclass(frozen=True)
class Decision:
    """Outcome of evaluating a tool call against the policy.

    ``behavior`` is the matched rule's behavior (or the default); ``allowed`` is
    the resolved go/no-go after applying ``ask`` resolution; ``reason`` is a short
    tag and ``rule`` the matched rule's serialized form (for logging).
    """

    behavior: Behavior
    allowed: bool
    reason: str
    rule: str | None = None


@runtime_checkable
class Policy(Protocol):
    def evaluate(self, tool_name: str, arguments: dict) -> Decision: ...


def _find_unescaped(text: str, ch: str) -> int:
    i = 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == ch:
            return i
        i += 1
    return -1


def parse_rule(text: str, behavior: Behavior) -> Rule:
    """Parse a ``Tool`` or ``Tool(content)`` rule string. Parens in content escape as ``\\(``."""
    text = text.strip()
    if text.endswith(")"):
        idx = _find_unescaped(text, "(")
        if idx > 0:
            tool = text[:idx].strip()
            content = text[idx + 1 : -1].replace("\\(", "(").replace("\\)", ")")
            return Rule(behavior, tool, content)
    return Rule(behavior, text, None)


def format_rule(rule: Rule) -> str:
    """Serialize a :class:`Rule` back to its ``Tool(content)`` string form."""
    if rule.content is None:
        return rule.tool_name
    escaped = rule.content.replace("(", "\\(").replace(")", "\\)")
    return f"{rule.tool_name}({escaped})"


def default_content_match(tool_name: str, rule: Rule, arguments: dict) -> bool:
    """Generic content match: the rule content appears in any argument value."""
    needle = (rule.content or "").lower()
    return any(needle in str(v).lower() for v in arguments.values())


class NullPolicy:
    """Permissive policy: everything allowed (engine disabled / baseline mode)."""

    def evaluate(self, tool_name: str, arguments: dict) -> Decision:
        return Decision(behavior="allow", allowed=True, reason="disabled")


class PolicyEngine:
    """Evaluates tool calls against allow/deny/ask rules with deny>ask>allow."""

    def __init__(
        self,
        rules: list[Rule],
        *,
        default_behavior: Behavior = "allow",
        ask_resolution: Behavior = "deny",
        content_matcher: ContentMatcher | None = None,
    ) -> None:
        self.rules = list(rules)
        self.default_behavior = default_behavior
        # How an "ask" resolves with no human in the loop: "deny" (secure) or "allow".
        self.ask_resolution = ask_resolution if ask_resolution in ("allow", "deny") else "deny"
        self.content_matcher = content_matcher or default_content_match

    def _matches(self, rule: Rule, tool_name: str, arguments: dict) -> bool:
        if rule.tool_name.lower() not in tool_name.lower():
            return False
        if rule.content is None:
            return True
        return self.content_matcher(tool_name, rule, arguments)

    def evaluate(self, tool_name: str, arguments: dict) -> Decision:
        matched = [r for r in self.rules if self._matches(r, tool_name, arguments)]
        for behavior in ("deny", "ask", "allow"):  # precedence: deny > ask > allow
            for rule in matched:
                if rule.behavior == behavior:
                    return self._decide(behavior, f"rule:{behavior}", format_rule(rule))
        return self._decide(self.default_behavior, "default", None)

    def _decide(self, behavior: Behavior, reason: str, rule: str | None) -> Decision:
        allowed = behavior == "allow" or (behavior == "ask" and self.ask_resolution == "allow")
        return Decision(behavior=behavior, allowed=allowed, reason=reason, rule=rule)
