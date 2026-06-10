"""Supervise a Dockerized Qdrant for `redcell serve` (the RAG backend).

Mirrors :class:`~redcell.gateway.GatewaySupervisor`, but the lifecycle is a
Docker container rather than a child process: ``start()`` runs
``docker compose up -d`` and waits for Qdrant's REST port, so the gateway's
``rag`` target has a store to connect to. Resilient by design — if Docker is
unavailable, the compose command fails, or the port never opens, it logs a
warning and leaves ``available`` False; ``serve`` still runs (RAG calls just
return errors) exactly as it does when the gateway is missing.

Unlike the gateway, Qdrant is **not** stopped on shutdown by default: it is a
persistent data service and ``up -d`` is detached on purpose, so it is normal to
leave it running across `serve` restarts. Set ``stop_on_exit`` for full parity.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("redcell.qdrant")


class QdrantSupervisor:
    """Start a Dockerized Qdrant via docker compose, health-check it, optionally stop it.

    Args:
        compose_file: path to the compose file defining the service (``-f``).
        service: the compose service name to bring up (e.g. ``qdrant``).
        host: host the Qdrant REST API binds (for the readiness probe).
        port: port the Qdrant REST API binds.
        ready_timeout: seconds to wait for the port to accept connections.
        stop_on_exit: if True, ``docker compose stop`` the service on shutdown.
    """

    def __init__(
        self,
        *,
        compose_file: str,
        service: str,
        host: str,
        port: int,
        ready_timeout: float = 30.0,
        stop_on_exit: bool = False,
    ) -> None:
        self._compose_file = compose_file
        self._service = service
        self._host = host
        self._port = port
        self._ready_timeout = ready_timeout
        self._stop_on_exit = stop_on_exit
        self.available = False

    async def start(self) -> None:
        if not await self._compose("up", "-d", self._service):
            return  # docker missing or compose failed; degrade to no-RAG
        if await self._wait_ready():
            self.available = True
            logger.info("qdrant ready on %s:%d", self._host, self._port)
        else:
            logger.warning(
                "qdrant not ready on %s:%d within %.0fs; continuing without it",
                self._host,
                self._port,
                self._ready_timeout,
            )

    async def stop(self) -> None:
        self.available = False
        if self._stop_on_exit:
            await self._compose("stop", self._service)

    async def _compose(self, *args: str) -> bool:
        """Run ``docker compose -f <file> <args>``; return True on success."""
        cmd = ["docker", "compose", "-f", self._compose_file, *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.warning(
                "docker not found; cannot run `compose %s` — continuing without the RAG store",
                " ".join(args),
            )
            return False
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = (stderr or b"").decode(errors="replace").strip()
            logger.warning(
                "`docker compose %s` failed (%s); continuing without qdrant",
                " ".join(args),
                detail[:200] or f"exit {proc.returncode}",
            )
            return False
        return True

    async def _wait_ready(self) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._ready_timeout
        while loop.time() < deadline:
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
