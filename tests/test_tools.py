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
    assert await reg.invoke("add", {"a": 2, "b": 3}) == "5"
    assert await reg.invoke("shout", {"text": "hi"}) == "HI"


async def test_invoke_unknown_tool_returns_error_string():
    reg = ToolRegistry([add])
    result = await reg.invoke("nope", {})
    assert "error" in result.lower()


async def test_tool_exception_is_caught_and_returned():
    @tool
    def boom() -> str:
        """Always fails."""
        raise ValueError("kaboom")

    reg = ToolRegistry([boom])
    result = await reg.invoke("boom", {})
    assert "kaboom" in result


def test_specs_returns_all_tools():
    reg = ToolRegistry([add, shout])
    names = {s["function"]["name"] for s in reg.specs()}
    assert names == {"add", "shout"}


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
    assert await reg.invoke("ping", {}) == "pong"
