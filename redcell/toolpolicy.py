"""Compose a content matcher routing shell/filesystem rules to aware matchers.

Plugs into :class:`~redcell.permissions.PolicyEngine` as its ``content_matcher``:
a rule scoped to a shell tool is matched command-aware (:mod:`redcell.shellpolicy`),
one scoped to a filesystem tool is matched path-aware (:mod:`redcell.pathpolicy`),
and anything else falls back to the engine's generic substring match.
"""

from __future__ import annotations

from .pathpolicy import path_rule_matches
from .permissions import ContentMatcher, Rule, default_content_match
from .shellpolicy import command_rule_matches, extract_command

# Tool-name substrings (case-insensitive) that select the command/path matchers.
DEFAULT_SHELL_TERMS = ("run_command", "run_script", "bash", "shell", "powershell")
DEFAULT_PATH_TERMS = (
    "read_file",
    "read_text_file",
    "read_media_file",
    "read_multiple_files",
    "write_file",
    "edit_file",
    "create_directory",
    "move_file",
    "list_directory",
    "directory_tree",
    "search_files",
    "get_file_info",
    "filesystem",
)


def make_content_matcher(
    shell_terms: tuple[str, ...] = DEFAULT_SHELL_TERMS,
    path_terms: tuple[str, ...] = DEFAULT_PATH_TERMS,
) -> ContentMatcher:
    """Return a content matcher dispatching by tool name to the aware matchers."""

    def matcher(tool_name: str, rule: Rule, arguments: dict) -> bool:
        name = tool_name.lower()
        content = rule.content or ""
        if any(term in name for term in shell_terms):
            command = extract_command(arguments)
            return bool(command) and command_rule_matches(rule.behavior, content, command)
        if any(term in name for term in path_terms):
            return path_rule_matches(content, arguments)
        return default_content_match(tool_name, rule, arguments)

    return matcher
