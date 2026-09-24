"""Render the effective AgentGateway config that `redcell serve` launches.

``agentgateway/config.yaml`` stays the file you own and edit. At startup redcell
writes a derived copy next to its other runtime state and points the gateway at
that instead, for two reasons the static file cannot handle on its own:

1. **One dead target takes every tool down.** AgentGateway aggregates all stdio
   targets behind a single MCP endpoint, and a target that exits during
   ``initialize`` fails the aggregated call. The client then sees zero tools, not
   "all tools but one". So a target that cannot start is dropped before launch:
   the SSH-launched sandbox targets when the sandbox is not up, and any target
   whose runtime (``npx``, ``uvx``, ``ssh``, …) is not on PATH.
2. **The Qdrant address depends on where redcell runs.** It is loopback in host
   mode and the ``qdrant`` service name inside the compose stack. AgentGateway has
   no env interpolation, so ``QDRANT_URL=`` arguments are rewritten from settings.

Rendering never blocks startup: if the source cannot be parsed or the copy
cannot be written, the source path is returned unchanged and AgentGateway
reports whatever is wrong with it.
"""

from __future__ import annotations

import copy
import logging
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("redcell.gatewayconfig")

Which = Callable[[str], str | None]


def _target_lists(config: dict[str, Any]) -> Iterator[list[dict[str, Any]]]:
    """Yield every ``mcp.targets`` list in the config (normally just one)."""
    for bind in config.get("binds") or []:
        for listener in bind.get("listeners") or []:
            for route in listener.get("routes") or []:
                for backend in route.get("backends") or []:
                    targets = (backend.get("mcp") or {}).get("targets")
                    if isinstance(targets, list):
                        yield targets


def _runtime(stdio: dict[str, Any]) -> list[str]:
    """Commands a stdio target needs on PATH.

    ``env VAR=x ... uvx server`` is a wrapper, so the first non-assignment
    argument after it is the real runtime and is checked too.
    """
    cmd = stdio.get("cmd")
    if not cmd:
        return []
    needed = [cmd]
    if Path(cmd).name == "env":
        for arg in stdio.get("args") or []:
            if "=" not in arg and not arg.startswith("-"):
                needed.append(arg)
                break
    return needed


def _uses_sandbox(stdio: dict[str, Any], ssh_config_path: str) -> bool:
    """True for a target launched over SSH through the generated sandbox config."""
    return Path(stdio.get("cmd") or "").name == "ssh" and ssh_config_path in (
        stdio.get("args") or []
    )


def _drop_reason(
    stdio: dict[str, Any], *, sandbox_available: bool, ssh_config_path: str, which: Which
) -> str | None:
    if _uses_sandbox(stdio, ssh_config_path) and not sandbox_available:
        return "execution sandbox is not available"
    missing = [cmd for cmd in _runtime(stdio) if which(cmd) is None]
    if missing:
        return f"{', '.join(missing)} not found on PATH"
    return None


def render_gateway_config(
    config: dict[str, Any],
    *,
    sandbox_available: bool,
    ssh_config_path: str,
    qdrant_url: str,
    which: Which = shutil.which,
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Return ``(effective_config, dropped)`` without mutating ``config``.

    ``dropped`` lists ``(target_name, reason)`` for each target left out.
    Non-stdio targets are passed through untouched.
    """
    rendered = copy.deepcopy(config)
    dropped: list[tuple[str, str]] = []
    for targets in _target_lists(rendered):
        kept = []
        for target in targets:
            stdio = target.get("stdio")
            if isinstance(stdio, dict):
                reason = _drop_reason(
                    stdio,
                    sandbox_available=sandbox_available,
                    ssh_config_path=ssh_config_path,
                    which=which,
                )
                if reason is not None:
                    dropped.append((str(target.get("name", "?")), reason))
                    continue
                args = stdio.get("args")
                if isinstance(args, list):
                    stdio["args"] = [
                        f"QDRANT_URL={qdrant_url}"
                        if isinstance(a, str) and a.startswith("QDRANT_URL=")
                        else a
                        for a in args
                    ]
            kept.append(target)
        targets[:] = kept
    return rendered, dropped


def write_effective_config(
    source_path: str | Path,
    out_path: str | Path,
    *,
    sandbox_available: bool,
    ssh_config_path: str,
    qdrant_url: str,
    which: Which = shutil.which,
) -> tuple[str, list[tuple[str, str]]]:
    """Render ``source_path`` to ``out_path``; return ``(path_to_launch, dropped)``.

    Falls back to ``(source_path, [])`` if the source cannot be read or parsed or
    the output cannot be written.
    """
    try:
        config = yaml.safe_load(Path(source_path).read_text())
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("could not parse %s (%s); launching it unmodified", source_path, exc)
        return str(source_path), []
    if not isinstance(config, dict):
        return str(source_path), []
    rendered, dropped = render_gateway_config(
        config,
        sandbox_available=sandbox_available,
        ssh_config_path=ssh_config_path,
        qdrant_url=qdrant_url,
        which=which,
    )
    out = Path(out_path)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# Generated by `redcell serve` from {source_path}. Edit that file, not this one.\n"
        )
        out.write_text(header + yaml.safe_dump(rendered, sort_keys=False))
    except OSError as exc:
        logger.warning("could not write %s (%s); launching %s unmodified", out, exc, source_path)
        return str(source_path), []
    return str(out), dropped
