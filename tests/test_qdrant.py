"""Tests for the Qdrant docker-compose supervisor (no real Docker needed)."""

import asyncio

from redcell.qdrant import QdrantSupervisor


def _closed_port() -> int:
    """A port nothing is listening on (bind, read, release)."""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _supervisor(**kw):
    base = dict(
        compose_file="docker-compose.yml",
        service="qdrant",
        host="127.0.0.1",
        port=6333,
        ready_timeout=0.2,
    )
    base.update(kw)
    return QdrantSupervisor(**base)


async def test_manage_false_skips_docker_but_still_waits(monkeypatch):
    # In the compose stack redcell has no Docker socket, so it must not shell out
    # to `docker compose` — but the readiness wait still has to happen, or a
    # misconfigured host/port looks like success.
    calls = []

    async def _fake_exec(*args, **kwargs):
        calls.append(args)
        raise AssertionError("manage=False must not run docker")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    # A closed port, so the result does not depend on whether a real Qdrant
    # happens to be running on the developer's machine.
    sup = _supervisor(manage=False, port=_closed_port(), stop_on_exit=True)
    await sup.start()
    assert calls == []
    assert sup.available is False  # readiness was probed, and timed out
    await sup.stop()  # must not try `docker compose stop` either
    assert calls == []


async def test_start_degrades_when_docker_missing(monkeypatch):
    async def _boom(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    sup = _supervisor()
    await sup.start()  # must not raise
    assert sup.available is False


async def test_start_degrades_when_compose_fails(monkeypatch):
    class _Proc:
        returncode = 1

        async def communicate(self):
            return b"", b"no configuration file provided"

    async def _fake_exec(*args, **kwargs):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    sup = _supervisor()
    await sup.start()
    assert sup.available is False


async def test_start_ready_when_compose_ok_and_port_open(monkeypatch):
    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def _fake_exec(*args, **kwargs):
        return _Proc()

    async def _fake_open(host, port):
        writer = type("_W", (), {"close": lambda self: None, "wait_closed": _noop})()
        return object(), writer

    async def _noop(self=None):
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(asyncio, "open_connection", _fake_open)
    sup = _supervisor()
    await sup.start()
    assert sup.available is True


async def test_stop_is_noop_without_stop_on_exit(monkeypatch):
    calls: list[tuple] = []

    async def _fake_exec(*args, **kwargs):
        calls.append(args)

        class _Proc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    sup = _supervisor(stop_on_exit=False)
    await sup.stop()
    assert calls == []  # no docker invocation
    assert sup.available is False

    sup_stop = _supervisor(stop_on_exit=True)
    await sup_stop.stop()
    assert any("stop" in a for a in calls[0])  # `docker compose ... stop qdrant`
