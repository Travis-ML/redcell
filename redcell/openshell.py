"""Supervise the OpenShell execution sandbox for `redcell serve`.

The ``shell`` and ``filesystem`` MCP servers run inside an OpenShell sandbox, not
on the redcell host. This module makes sure that sandbox exists and is reachable
before AgentGateway tries to launch those servers, and writes the SSH config they
are launched through.

Mirrors :class:`~redcell.qdrant.QdrantSupervisor`: the lifecycle is a
gateway-managed container rather than a child process, and every step is
best-effort. A missing ``openshell`` binary, an unreachable gateway, or a sandbox
that won't start logs a warning and leaves ``available`` False; ``serve`` still
runs and the two affected tools just error, exactly as they do today when the
execution VM is down.

Why SSH rather than ``openshell sandbox exec``: ``exec`` buffers stdin until EOF,
so a long-lived JSON-RPC session over it never gets a reply. SSH streams in both
directions. See sandbox/README.md.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

logger = logging.getLogger("redcell.openshell")


def ssh_host_alias(sandbox: str, workspace: str = "default") -> str:
    """The Host name ``openshell sandbox ssh-config`` emits for a sandbox.

    Kept here rather than parsed out of the generated file so the gateway config
    can hardcode the same value; ``agentgateway/config.yaml`` has no way to read
    it at runtime.
    """
    return f"openshell-{sandbox}.{workspace}"


class OpenShellSupervisor:
    """Ensure an OpenShell sandbox exists and is reachable over SSH.

    Args:
        bin: the ``openshell`` CLI executable.
        gateway_url: the gateway's control-plane URL, registered with the CLI.
        health_url: the gateway's health endpoint. A separate URL because the
            gateway serves health on its own port (8081), not the gRPC port.
        gateway_name: local name to register ``gateway_url`` under.
        sandbox: sandbox name to create or reuse.
        workspace: OpenShell workspace the sandbox lives in.
        image: sandbox image reference, built locally from ``sandbox/``.
        policy_path: sandbox policy YAML applied at creation.
        ssh_config_path: where to write the generated SSH client config that
            ``agentgateway/config.yaml`` points its ``ssh -F`` at.
        ready_timeout: seconds to wait for the gateway health endpoint.
        create_timeout: seconds to allow for sandbox creation. Generous by
            default because a cold first run pulls the supervisor and sandbox
            images.
        delete_on_exit: delete the sandbox on shutdown. Off by default — reusing
            it across restarts skips image pulls and keeps /sandbox contents.
    """

    def __init__(
        self,
        *,
        bin: str = "openshell",
        gateway_url: str,
        health_url: str,
        gateway_name: str,
        sandbox: str,
        workspace: str = "default",
        image: str,
        policy_path: str,
        ssh_config_path: str,
        ready_timeout: float = 60.0,
        create_timeout: float = 600.0,
        delete_on_exit: bool = False,
    ) -> None:
        self._bin = bin
        self._gateway_url = gateway_url
        self._health_url = health_url
        self._gateway_name = gateway_name
        self._sandbox = sandbox
        self._workspace = workspace
        self._image = image
        self._policy_path = policy_path
        self._ssh_config_path = ssh_config_path
        self._ready_timeout = ready_timeout
        self._create_timeout = create_timeout
        self._delete_on_exit = delete_on_exit
        self.available = False

    @property
    def ssh_host(self) -> str:
        return ssh_host_alias(self._sandbox, self._workspace)

    async def start(self) -> None:
        if shutil.which(self._bin) is None:
            logger.warning(
                "%r not found on PATH; shell/filesystem tools will be unavailable "
                "(install: uv tool install openshell)",
                self._bin,
            )
            return
        if not await self._wait_gateway_ready():
            logger.warning(
                "OpenShell gateway not healthy at %s within %.0fs; "
                "continuing without the execution sandbox",
                self._health_url,
                self._ready_timeout,
            )
            return
        # Registration is idempotent in effect: re-adding an existing name is
        # harmless, so a failure here is only worth a debug line as long as the
        # sandbox calls below succeed.
        await self._cli(
            "gateway", "add", self._gateway_url, "--name", self._gateway_name, "--local"
        )
        if not await self._ensure_sandbox():
            return
        if not await self._write_ssh_config():
            return
        self.available = True
        logger.info(
            "OpenShell sandbox %r ready; shell/filesystem reachable at %s",
            self._sandbox,
            self.ssh_host,
        )

    async def stop(self) -> None:
        self.available = False
        if self._delete_on_exit:
            await self._cli("sandbox", "delete", self._sandbox)

    async def _ensure_sandbox(self) -> bool:
        """Create the sandbox unless it already exists. Returns True if usable."""
        ok, _, _ = await self._cli("sandbox", "get", self._sandbox)
        if ok:
            logger.info("reusing existing OpenShell sandbox %r", self._sandbox)
            return True
        logger.info("creating OpenShell sandbox %r from %s", self._sandbox, self._image)
        ok, _, stderr = await self._cli(
            "sandbox",
            "create",
            "--name",
            self._sandbox,
            "--from",
            self._image,
            "--policy",
            self._policy_path,
            "--no-tty",
            # The sandbox outlives this command; it is `exec`'d and SSH'd into
            # afterwards, so the initial command only has to succeed and exit.
            "--",
            "true",
            timeout=self._create_timeout,
        )
        if not ok:
            logger.warning(
                "could not create OpenShell sandbox %r (%s); "
                "shell/filesystem tools will be unavailable. Is the image built? "
                "`docker build -t %s sandbox/`",
                self._sandbox,
                _brief(stderr),
                self._image,
            )
            return False
        return True

    async def _write_ssh_config(self) -> bool:
        """Generate the SSH client config AgentGateway launches the servers through."""
        ok, stdout, stderr = await self._cli("sandbox", "ssh-config", self._sandbox)
        if not ok or not stdout.strip():
            logger.warning(
                "could not generate an SSH config for sandbox %r (%s); "
                "shell/filesystem tools will be unavailable",
                self._sandbox,
                _brief(stderr),
            )
            return False
        path = Path(self._ssh_config_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(stdout)
            # Contains no secrets (the gateway authenticates the tunnel), but ssh
            # refuses a config it considers group- or world-writable.
            path.chmod(0o600)
        except OSError as exc:
            logger.warning("could not write %s (%s); shell/filesystem unavailable", path, exc)
            return False
        return True

    async def _wait_gateway_ready(self) -> bool:
        """Poll the gateway's health endpoint until it answers or time runs out."""
        import httpx

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._ready_timeout
        async with httpx.AsyncClient(timeout=5.0) as client:
            while loop.time() < deadline:
                try:
                    resp = await client.get(self._health_url)
                except Exception:  # not up yet, or DNS not resolving
                    await asyncio.sleep(0.5)
                    continue
                if resp.status_code == 200:
                    return True
                await asyncio.sleep(0.5)
        return False

    async def _cli(self, *args: str, timeout: float = 60.0) -> tuple[bool, str, str]:
        """Run ``openshell <args>``; return (ok, stdout, stderr). Never raises."""
        cmd = [self._bin, "--workspace", self._workspace, *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.warning("%r disappeared from PATH mid-startup", self._bin)
            return False, "", "binary not found"
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("`%s` timed out after %.0fs", " ".join(args), timeout)
            return False, "", f"timed out after {timeout:.0f}s"
        stdout = (out or b"").decode(errors="replace")
        stderr = (err or b"").decode(errors="replace")
        return proc.returncode == 0, stdout, stderr


def _brief(text: str) -> str:
    """Collapse a CLI error to one short line for a log message."""
    flat = " ".join(text.split())
    return flat[:200] or "no output"
