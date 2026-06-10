"""Path policy: path-aware matching + confinement for the permission engine.

For filesystem tools, a permission rule's content is a path or directory: a rule
``read_file(/etc)`` should match any read under ``/etc``, and confinement is just
``AGENT_PERMISSION_ALLOW=read_file(<sandbox>)`` with a deny/ask default. This
module canonicalizes paths (resolving ``..`` and ``~``) so an allowlist can't be
dodged by ``../`` traversal, and flags shell-expansion syntax (``$HOME``, ``~root``,
backticks, leading ``=``) that would make the validated path differ from the
executed one.
"""

from __future__ import annotations

import os
import re

# Shell/zsh expansion syntax: a path containing these is validated-as-one-thing,
# executed-as-another, so it can't be trusted against a literal allowlist.
_EXPANSION = re.compile(r"[$`]|^=|~[A-Za-z+]")


def has_expansion_syntax(path: str) -> bool:
    """True if the path contains shell expansion that defeats literal matching."""
    return bool(_EXPANSION.search(path))


def normalize_path(path: str) -> str:
    """Canonical absolute path: expand ``~``, resolve ``..`` and symlinks."""
    return os.path.realpath(os.path.expanduser(path))


def within_root(path: str, root: str) -> bool:
    """True if ``path`` is ``root`` or lives under it (after canonicalization)."""
    base = normalize_path(root)
    target = normalize_path(path)
    return target == base or target.startswith(base + os.sep)


def extract_path(arguments: dict) -> str | None:
    """Pull the path string out of a filesystem tool call's arguments."""
    for key in ("path", "file_path", "filepath", "source", "directory", "dir", "filename"):
        val = arguments.get(key)
        if isinstance(val, str):
            return val
    for val in arguments.values():  # fall back to the first string that looks path-like
        if isinstance(val, str) and ("/" in val or val.startswith("~")):
            return val
    return None


def path_rule_matches(rule_content: str, arguments: dict) -> bool:
    """Whether a filesystem rule (a path/dir) matches the call's path argument.

    A path bearing expansion syntax always matches (so a deny rule catches the
    suspicious call); otherwise the call's path must equal or sit under the rule's
    path after canonicalization.
    """
    target = extract_path(arguments)
    if target is None:
        return False
    if has_expansion_syntax(target):
        return True
    return within_root(target, rule_content)
