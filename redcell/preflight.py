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

import os
import re
import shutil
import socket
from collections.abc import Mapping
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


# Provider prefix (LiteLLM format) -> the environment variable holding its key.
_PROVIDER_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "groq": "GROQ_API_KEY",
}
# Self-hosted providers that can only be reached through AGENT_API_BASE.
_NEEDS_API_BASE = {"hosted_vllm"}


def check_model(
    model: str, *, api_base: str | None, environ: Mapping[str, str] | None = None
) -> Check:
    """Check the model has what it needs to be called: a provider key or an endpoint.

    An ``api_base`` means an OpenAI-compatible server the user runs, which
    satisfies any provider. Unknown providers pass: LiteLLM supports far more
    than this table and the first request reports anything missing.
    """
    env = os.environ if environ is None else environ
    name = f"model ({model})"
    provider = model.split("/", 1)[0] if "/" in model else ""
    if api_base:
        return Check(name, True)
    if provider in _NEEDS_API_BASE:
        return Check(name, False, "set AGENT_API_BASE to your server, e.g. http://HOST:8000/v1")
    key = _PROVIDER_KEYS.get(provider)
    if key and not env.get(key):
        return Check(name, False, f"set {key} in .env")
    return Check(name, True)


def port_in_use(host: str, port: int) -> bool:
    """True if something accepts TCP connections on ``host:port``."""
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False
