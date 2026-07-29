"""Startup preflight for the MCP stack.

Two complementary checks, both informational (redcell degrades gracefully when a
runtime is missing — the affected MCP target just contributes no tools):

1. :func:`check_runtimes` — are the external commands the gateway's stdio MCP
   servers need present on PATH (``agentgateway``, ``npx``, ``uvx``, ``docker``)?
2. :func:`target_tool_counts` — after the gateway connects, how many tools did
   each declared target actually produce? A target with zero tools is down (its
   MCP server failed to start, e.g. a missing runtime or an unreachable VM).

This turns a silent gap ("playwright tools just aren't there") into an explicit,
actionable line at startup.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Check:
    """One runtime probe result; ``hint`` is shown when ``ok`` is False."""

    name: str
    ok: bool
    hint: str = ""


def check_runtimes(
    *,
    gateway_bin: str = "agentgateway",
    openshell_bin: str = "openshell",
    check_gateway: bool = True,
    check_node: bool = True,
    check_uv: bool = True,
    check_docker: bool = True,
    check_openshell: bool = True,
    check_ssh: bool = True,
) -> list[Check]:
    """Check the external commands the MCP stack relies on are on PATH.

    The flags let the caller skip checks that don't apply to its config (e.g. no
    Docker check when Qdrant autostart is off).
    """

    def present(cmd: str, hint: str) -> Check:
        return Check(cmd, shutil.which(cmd) is not None, hint)

    checks: list[Check] = []
    if check_gateway:
        checks.append(present(gateway_bin, "install AgentGateway — https://agentgateway.dev"))
    if check_node:
        checks.append(present("npx", "install Node.js — runs the playwright MCP server"))
    if check_uv:
        checks.append(
            present("uvx", "install uv — runs the fetch + qdrant MCP servers (astral.sh/uv)")
        )
    if check_docker:
        checks.append(present("docker", "install Docker — runs the Qdrant RAG store"))
    if check_openshell:
        checks.append(
            present(
                openshell_bin,
                "install OpenShell — `uv tool install openshell` (execution sandbox)",
            )
        )
    if check_ssh:
        # The shell/filesystem MCP servers are launched over SSH into the sandbox,
        # so a missing ssh client silently removes both.
        checks.append(present("ssh", "install an OpenSSH client — reaches the execution sandbox"))
    return checks


def parse_target_names(config_path: str | Path) -> list[str]:
    """Best-effort list of MCP target names declared in the gateway config.

    Matches ``- name: <x>`` lines (the only place names appear in the bundled
    config). Returns [] if the file can't be read, so callers degrade to a plain
    tool count.
    """
    try:
        text = Path(config_path).read_text()
    except OSError:
        return []
    return re.findall(r"^\s*-\s*name:\s*([\w-]+)", text, flags=re.MULTILINE)


def target_tool_counts(tool_names: list[str], targets: list[str]) -> dict[str, int]:
    """Count discovered tools per target by substring match (separator-agnostic).

    The gateway namespaces a target's tools with the target name (e.g.
    ``shell_run_command``, ``rag_qdrant-find``), so a case-insensitive substring
    test attributes each tool without assuming a particular delimiter. Preserves
    ``targets`` order; a target with 0 is down.
    """
    counts = {t: 0 for t in targets}
    for name in tool_names:
        low = name.lower()
        for t in targets:
            if t.lower() in low:
                counts[t] += 1
                break
    return counts
