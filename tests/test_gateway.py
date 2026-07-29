# tests/test_gateway.py
"""GatewaySupervisor lifecycle, tested with a stub subprocess (no real gateway)."""

import socket
import sys

from redcell.gateway import GatewaySupervisor


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_missing_binary_degrades():
    sup = GatewaySupervisor(
        command=["definitely-not-a-real-binary-xyz", "-f", "cfg.yaml"],
        host="127.0.0.1",
        port=_free_port(),
        ready_timeout=1.0,
    )
    await sup.start()
    assert sup.available is False
    await sup.stop()  # no-op, no process


async def test_starts_detects_ready_then_stops():
    port = _free_port()
    # Stub "gateway": a python process that binds the port and sleeps.
    script = (
        "import socket,time;"
        "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
        f"s.bind(('127.0.0.1',{port}));s.listen();time.sleep(30)"
    )
    sup = GatewaySupervisor(
        command=[sys.executable, "-c", script],
        host="127.0.0.1",
        port=port,
        ready_timeout=10.0,
    )
    await sup.start()
    assert sup.available is True
    await sup.stop()
    assert sup.available is False


async def test_process_that_exits_is_not_ready():
    port = _free_port()
    sup = GatewaySupervisor(
        command=[sys.executable, "-c", "raise SystemExit(1)"],
        host="127.0.0.1",
        port=port,
        ready_timeout=3.0,
    )
    await sup.start()
    assert sup.available is False
    await sup.stop()  # no-op: process already exited
