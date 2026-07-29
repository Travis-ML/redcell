"""Tests for the OpenShell sandbox supervisor (no real gateway or Docker needed)."""

import asyncio
import shutil

import pytest

from redcell.openshell import OpenShellSupervisor, ssh_host_alias

SSH_CONFIG = """Host openshell-redcell-sbx.default
    User sandbox
    ProxyCommand /usr/local/bin/openshell ssh-proxy --gateway-name redcell --name redcell-sbx
"""


def _supervisor(tmp_path, **kw):
    base = dict(
        gateway_url="http://127.0.0.1:8080",
        health_url="http://127.0.0.1:8081/healthz",
        gateway_name="redcell",
        sandbox="redcell-sbx",
        image="redcell-sandbox:local",
        policy_path="sandbox/policy.yaml",
        ssh_config_path=str(tmp_path / ".redcell" / "openshell_ssh_config"),
        ready_timeout=0.2,
        create_timeout=1.0,
    )
    base.update(kw)
    return OpenShellSupervisor(**base)


@pytest.fixture
def cli(monkeypatch):
    """Record `openshell` invocations and control each one's outcome.

    ``results`` maps a subcommand prefix (e.g. "sandbox get") to
    (returncode, stdout, stderr); anything unmatched succeeds silently.
    """

    class Recorder:
        def __init__(self):
            self.calls: list[list[str]] = []
            self.results: dict[str, tuple[int, bytes, bytes]] = {}

    rec = Recorder()
    monkeypatch.setattr(shutil, "which", lambda _cmd: "/usr/local/bin/openshell")

    async def fake_exec(*cmd, **kwargs):
        rec.calls.append(list(cmd))
        joined = " ".join(cmd)
        rc, out, err = 0, b"", b""
        for prefix, result in rec.results.items():
            if prefix in joined:
                rc, out, err = result
                break

        class _Proc:
            returncode = rc

            async def communicate(self):
                return out, err

            def kill(self):
                pass

            async def wait(self):
                return rc

        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return rec


@pytest.fixture
def healthy(monkeypatch):
    """Make the gateway health probe succeed."""
    _patch_health(monkeypatch, 200)


def _patch_health(monkeypatch, status):
    import httpx

    class _Resp:
        status_code = status

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, _url):
            if status is None:
                raise httpx.ConnectError("refused")
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


def test_ssh_host_alias():
    assert ssh_host_alias("redcell-sbx") == "openshell-redcell-sbx.default"
    assert ssh_host_alias("box", "team") == "openshell-box.team"


async def test_degrades_when_cli_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)
    sup = _supervisor(tmp_path)
    await sup.start()  # must not raise
    assert sup.available is False


async def test_degrades_when_gateway_unhealthy(tmp_path, monkeypatch, cli):
    _patch_health(monkeypatch, None)
    sup = _supervisor(tmp_path)
    await sup.start()
    assert sup.available is False
    # Must not have gone on to touch the sandbox once the gateway looked down.
    assert not any("sandbox" in " ".join(c) for c in cli.calls)


async def test_reuses_existing_sandbox(tmp_path, cli, healthy):
    cli.results["sandbox ssh-config"] = (0, SSH_CONFIG.encode(), b"")
    sup = _supervisor(tmp_path)
    await sup.start()
    assert sup.available is True
    joined = [" ".join(c) for c in cli.calls]
    assert any("sandbox get" in c for c in joined)
    assert not any("sandbox create" in c for c in joined)


async def test_creates_sandbox_when_absent(tmp_path, cli, healthy):
    cli.results["sandbox get"] = (1, b"", b"sandbox not found")
    cli.results["sandbox ssh-config"] = (0, SSH_CONFIG.encode(), b"")
    sup = _supervisor(tmp_path)
    await sup.start()
    assert sup.available is True
    create = next(c for c in cli.calls if "create" in c)
    assert "--from" in create and "redcell-sandbox:local" in create
    assert "--policy" in create and "sandbox/policy.yaml" in create


async def test_degrades_when_create_fails(tmp_path, cli, healthy):
    cli.results["sandbox get"] = (1, b"", b"sandbox not found")
    cli.results["sandbox create"] = (1, b"", b"policy contains unsafe content")
    sup = _supervisor(tmp_path)
    await sup.start()
    assert sup.available is False


async def test_writes_ssh_config(tmp_path, cli, healthy):
    cli.results["sandbox ssh-config"] = (0, SSH_CONFIG.encode(), b"")
    sup = _supervisor(tmp_path)
    await sup.start()
    written = tmp_path / ".redcell" / "openshell_ssh_config"
    assert written.read_text() == SSH_CONFIG
    # ssh rejects a config file others can write.
    assert written.stat().st_mode & 0o077 == 0
    assert sup.ssh_host == "openshell-redcell-sbx.default"


async def test_degrades_when_ssh_config_empty(tmp_path, cli, healthy):
    # A zero-exit-but-empty ssh-config would otherwise leave `available` True and
    # an empty config file, and every tool call would fail with a confusing
    # "Could not resolve hostname".
    cli.results["sandbox ssh-config"] = (0, b"", b"")
    sup = _supervisor(tmp_path)
    await sup.start()
    assert sup.available is False


async def test_stop_leaves_sandbox_by_default(tmp_path, cli, healthy):
    cli.results["sandbox ssh-config"] = (0, SSH_CONFIG.encode(), b"")
    sup = _supervisor(tmp_path)
    await sup.start()
    cli.calls.clear()
    await sup.stop()
    assert sup.available is False
    assert cli.calls == []


async def test_stop_deletes_sandbox_when_requested(tmp_path, cli, healthy):
    cli.results["sandbox ssh-config"] = (0, SSH_CONFIG.encode(), b"")
    sup = _supervisor(tmp_path, delete_on_exit=True)
    await sup.start()
    cli.calls.clear()
    await sup.stop()
    assert any("delete" in c for c in cli.calls)
