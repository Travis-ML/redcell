"""Tests for Tool schema generation and explicit overrides."""

from redcell.tools import Tool, ToolRegistry, tool


@tool
def add(a: int, b: int) -> int:
    """Add two integers.

    Args:
        a: first addend
        b: second addend
    """
    return a + b


@tool
async def shout(text: str) -> str:
    """Return TEXT uppercased."""
    return text.upper()


def test_schema_generated_from_signature():
    spec = add.spec()
    assert spec["type"] == "function"
    fn = spec["function"]
    assert fn["name"] == "add"
    assert "Add two integers" in fn["description"]
    assert fn["parameters"]["properties"]["a"]["type"] == "integer"
    assert fn["parameters"]["properties"]["b"]["type"] == "integer"
    assert set(fn["parameters"]["required"]) == {"a", "b"}


async def test_registry_invokes_sync_and_async_tools():
    reg = ToolRegistry([add, shout])
    r1 = await reg.invoke("add", {"a": 2, "b": 3})
    assert r1.content == "5" and not r1.is_error
    r2 = await reg.invoke("shout", {"text": "hi"})
    assert r2.content == "HI" and not r2.is_error


async def test_invoke_unknown_tool_returns_error_result():
    reg = ToolRegistry([add])
    result = await reg.invoke("nope", {})
    assert result.is_error
    assert "nope" in result.content
    assert result.content.startswith("<tool_use_error>")


async def test_tool_exception_is_caught_and_flagged():
    @tool
    def boom() -> str:
        """Always fails."""
        raise ValueError("kaboom")

    reg = ToolRegistry([boom])
    result = await reg.invoke("boom", {})
    assert result.is_error
    assert "kaboom" in result.content
    assert "<tool_use_error>" in result.content


async def test_large_result_is_truncated():
    from redcell.tools import _MAX_TOOL_RESULT_CHARS

    @tool
    def big() -> str:
        """Returns a huge string."""
        return "x" * (_MAX_TOOL_RESULT_CHARS + 5000)

    result = await ToolRegistry([big]).invoke("big", {})
    assert not result.is_error
    assert len(result.content) <= _MAX_TOOL_RESULT_CHARS + 64  # + truncation marker
    assert "chars truncated" in result.content


def test_classification_defaults_are_fail_closed():
    # An unannotated tool is assumed mutating, not concurrency-safe, destructive.
    @tool
    def bare() -> str:
        """No classification."""
        return "x"

    assert bare.is_read_only({}) is False  # assumed mutating
    assert bare.is_concurrency_safe({}) is False  # assumed serial-only
    assert bare.is_destructive({}) is False  # not declared destructive
    assert bare.source == "builtin"


def test_classification_flags_and_predicates():
    @tool(read_only=True, concurrency_safe=True)
    def ro() -> str:
        """Read only."""
        return "x"

    assert ro.is_read_only({}) and ro.is_concurrency_safe({})

    # Predicate form: read-only only when arg 'mode' == 'read'.
    def ro_pred(args):
        return args.get("mode") == "read"

    t = Tool(lambda **k: "x", name="cond", read_only=ro_pred)
    assert t.is_read_only({"mode": "read"})
    assert not t.is_read_only({"mode": "write"})


def test_classifier_that_raises_fails_closed():
    def boom(_args):
        raise RuntimeError("nope")

    t = Tool(lambda **k: "x", name="c", read_only=boom, concurrency_safe=boom, destructive=boom)
    assert t.is_read_only({}) is False  # error -> not read-only
    assert t.is_concurrency_safe({}) is False  # error -> not parallel-safe
    assert t.is_destructive({}) is True  # error -> assume destructive


def test_specs_returns_all_tools():
    reg = ToolRegistry([add, shout])
    names = {s["function"]["name"] for s in reg.specs()}
    assert names == {"add", "shout"}


async def test_register_no_overwrite_shadows_collision():
    builtin = Tool(lambda **k: "builtin", name="dup", source="builtin")
    mcp = Tool(lambda **k: "mcp", name="dup", source="mcp")
    reg = ToolRegistry([builtin])
    # overwrite=False refuses the collision: the builtin survives.
    added = reg.register(mcp, overwrite=False)
    assert added is False
    assert reg.names() == {"dup"}
    assert (await reg.invoke("dup", {})).content == "builtin"
    # default overwrite=True still replaces (unchanged behavior).
    assert reg.register(mcp) is True
    assert (await reg.invoke("dup", {})).content == "mcp"


def test_tool_derives_name_and_description_from_function():
    @tool
    def greet(name: str) -> str:
        """Say hello."""
        return f"hi {name}"

    assert greet.name == "greet"
    assert greet.description == "Say hello."


async def test_tool_accepts_name_and_description_overrides():
    async def fn(**kwargs):
        return "ok"

    t = Tool(
        fn,
        schema={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        name="remote_tool",
        description="A remote tool.",
    )
    assert t.name == "remote_tool"
    assert t.description == "A remote tool."
    spec = t.spec()
    assert spec["function"]["name"] == "remote_tool"
    assert spec["function"]["parameters"]["properties"]["x"]["type"] == "string"
    assert await t.call({"x": "y"}) == "ok"


def test_tool_empty_description_override_is_honored():
    # An explicit "" must not fall back to the docstring (the `is not None` guard).
    def fn() -> str:
        """Docstring that should be ignored."""
        return "x"

    t = Tool(fn, name="remote", description="")
    assert t.description == ""


async def test_registry_invoke_uses_overridden_name():
    async def fn(**kwargs):
        return "pong"

    t = Tool(fn, schema={"type": "object", "properties": {}, "required": []}, name="ping")
    reg = ToolRegistry([t])
    assert (await reg.invoke("ping", {})).content == "pong"
