"""Tests for the MCP startup preflight helpers."""

from pathlib import Path

import redcell.preflight as preflight
from redcell.preflight import (
    check_runtimes,
    parse_target_names,
    target_tool_counts,
)


def test_check_runtimes_reports_presence(monkeypatch):
    present = {"agentgateway", "npx", "uvx"}  # docker "missing"
    monkeypatch.setattr(preflight.shutil, "which", lambda cmd: cmd if cmd in present else None)

    checks = check_runtimes()
    by_name = {c.name: c for c in checks}
    assert by_name["agentgateway"].ok
    assert by_name["npx"].ok
    assert by_name["uvx"].ok
    assert not by_name["docker"].ok
    assert by_name["docker"].hint  # an actionable hint is present


def test_check_runtimes_honors_flags(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda cmd: None)
    checks = check_runtimes(
        check_node=False,
        check_uv=False,
        check_docker=False,
        check_openshell=False,
        check_ssh=False,
    )
    assert [c.name for c in checks] == ["agentgateway"]


def test_check_runtimes_custom_gateway_bin(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda cmd: cmd)
    checks = check_runtimes(
        gateway_bin="my-gw",
        check_node=False,
        check_uv=False,
        check_docker=False,
        check_openshell=False,
        check_ssh=False,
    )
    assert checks[0].name == "my-gw" and checks[0].ok


def test_parse_target_names_reads_bundled_config():
    names = parse_target_names("agentgateway/config.yaml")
    # The bundled gateway config declares these five MCP targets.
    assert names == ["playwright", "filesystem", "fetch", "rag", "shell"]


def test_parse_target_names_missing_file_is_empty(tmp_path: Path):
    assert parse_target_names(tmp_path / "nope.yaml") == []


def test_target_tool_counts_groups_namespaced_tools():
    targets = ["playwright", "filesystem", "fetch", "rag", "shell"]
    discovered = [
        "playwright_browser_navigate",
        "playwright_browser_click",
        "fetch_fetch",
        "rag_qdrant-find",
        "rag_qdrant-store",
        "filesystem_read_file",
    ]
    counts = target_tool_counts(discovered, targets)
    assert counts == {
        "playwright": 2,
        "filesystem": 1,
        "fetch": 1,
        "rag": 2,
        "shell": 0,  # no tools -> server is down
    }


def test_target_tool_counts_no_targets():
    assert target_tool_counts(["a_x", "b_y"], []) == {}
