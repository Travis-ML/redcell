from typer.testing import CliRunner

from redcell.cli import (
    _DEFAULT_MCP_READONLY,
    _mcp_readonly_terms,
    app,
    apply_denylist,
    mark_readonly_mcp_tools,
    unmatched_denylist,
)
from redcell.config import Settings
from redcell.tools import Tool


def _named_tool(name: str) -> Tool:
    async def fn(**kwargs):
        return "ok"

    fn.__name__ = name
    return Tool(fn, schema={"type": "object", "properties": {}, "required": []}, name=name)


runner = CliRunner()


def test_version_command():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


def test_denylist_matches_namespaced_tool_by_target_name():
    # The exact-match bug: a 'shell' target exposes namespaced tools like
    # 'shell_run_command'; substring matching must drop them.
    tools = [
        _named_tool("shell_run_command"),
        _named_tool("shell_run_script"),
        _named_tool("web_search"),
    ]
    kept, dropped = apply_denylist(tools, {"shell"})
    assert sorted(dropped) == ["shell_run_command", "shell_run_script"]
    assert [t.name for t in kept] == ["web_search"]


def test_denylist_matches_exact_tool_name():
    tools = [_named_tool("run_command"), _named_tool("add")]
    kept, dropped = apply_denylist(tools, {"run_command"})
    assert dropped == ["run_command"]
    assert [t.name for t in kept] == ["add"]


def test_denylist_is_case_insensitive():
    kept, dropped = apply_denylist([_named_tool("Filesystem_Read")], {"filesystem"})
    assert dropped == ["Filesystem_Read"]
    assert kept == []


def test_unmatched_denylist_flags_terms_that_hit_nothing():
    names = ["shell_run_command", "fetch"]
    # 'shell' matches; 'filesystem' matches nothing -> reported as stale.
    assert unmatched_denylist(names, {"shell", "filesystem"}) == ["filesystem"]


def test_unmatched_denylist_empty_when_all_match():
    assert unmatched_denylist(["shell_x", "fetch_y"], {"shell", "fetch"}) == []


def test_mcp_readonly_terms_default_when_unset():
    assert _mcp_readonly_terms(Settings(mcp_readonly_tools="")) == set(_DEFAULT_MCP_READONLY)


def test_mcp_readonly_terms_override_replaces_default():
    terms = _mcp_readonly_terms(Settings(mcp_readonly_tools="qdrant-find, Fetch"))
    assert terms == {"qdrant-find", "fetch"}  # lowercased, default replaced


def test_mark_readonly_flags_matching_mcp_tools_only():
    # Read-only tools (namespaced or bare) match by substring; mutating ones don't.
    ro1 = Tool(lambda **k: "x", name="mcp__rag__qdrant-find", source="mcp")
    ro2 = Tool(lambda **k: "x", name="read_file", source="mcp")
    mut1 = Tool(lambda **k: "x", name="qdrant-store", source="mcp")
    mut2 = Tool(lambda **k: "x", name="shell_run_command", source="mcp")
    marked = mark_readonly_mcp_tools([ro1, ro2, mut1, mut2], set(_DEFAULT_MCP_READONLY))
    assert set(marked) == {"mcp__rag__qdrant-find", "read_file"}
    assert ro1.is_read_only({}) and ro1.is_concurrency_safe({})
    assert ro2.is_concurrency_safe({})
    # Mutating tools keep the conservative serial defaults.
    assert not mut1.is_concurrency_safe({})
    assert not mut2.is_concurrency_safe({})
