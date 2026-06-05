"""Supervise the AgentGateway child process for `redcell serve`.

Spawns the gateway, polls its MCP proxy port until ready, and terminates it on
shutdown. Resilient: a missing binary or a gateway that never becomes ready logs
a warning and leaves ``available`` False, so the server still runs with builtins.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("redcell.gateway")


class GatewaySupervisor:
    """Start, health-check, and stop the AgentGateway process.

    Args:
        command: the full argv to spawn, e.g. ``["agentgateway", "-f", "config.yaml"]``.
        host: host the gateway MCP proxy binds (for the readiness probe).
        port: port the gateway MCP proxy binds.
        ready_timeout: seconds to wait for the port to accept connections.
    """

    def __init__(
        self,
        command: list[str],
        host: str,
        port: int,
        ready_timeout: float = 30.0,
    ) -> None:
        self._command = list(command)
        self._host = host
        self._port = port
        self._ready_timeout = ready_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self.available = False

    async def start(self) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(*self._command)
        except FileNotFoundError:
            logger.warning(
                "gateway binary %r not found; continuing without gateway",
                self._command[0],
            )
            return
        if await self._wait_ready():
            self.available = True
            logger.info("gateway ready on %s:%d", self._host, self._port)
        else:
            logger.warning(
                "gateway not ready on %s:%d within %.0fs; continuing without it",
                self._host,
                self._port,
                self._ready_timeout,
            )
            await self.stop()

    async def _wait_ready(self) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._ready_timeout
        while loop.time() < deadline:
            if self._proc is not None and self._proc.returncode is not None:
                return False  # process exited before binding
            try:
                _, writer = await asyncio.open_connection(self._host, self._port)
            except OSError:
                await asyncio.sleep(0.25)
                continue
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return True
        return False

    async def stop(self) -> None:
        self.available = False
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            proc.kill()
            await proc.wait()
