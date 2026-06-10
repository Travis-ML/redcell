"""Tests for the SearXNG web_search tool. No network: uses httpx.MockTransport."""

import httpx

from redcell.searxng import make_web_search
from redcell.tools import ToolRegistry

SAMPLE = {
    "answers": [{"answer": "42", "url": "http://ex/a"}],
    "results": [
        {"title": "First", "url": "http://ex/1", "content": "first snippet"},
        {"title": "Second", "url": "http://ex/2", "content": "second snippet"},
        {"title": "Third", "url": "http://ex/3", "content": "third snippet"},
    ],
}


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_web_search_formats_results():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["q"] = request.url.params.get("q")
        captured["format"] = request.url.params.get("format")
        return httpx.Response(200, json=SAMPLE)

    client = _client(handler)
    t = make_web_search("http://searx", client=client)
    out = await t.call({"query": "meaning of life"})
    await client.aclose()

    assert captured["path"] == "/search"
    assert captured["q"] == "meaning of life"
    assert captured["format"] == "json"
    assert "Answer: 42" in out
    assert "First" in out and "http://ex/1" in out and "first snippet" in out


async def test_web_search_respects_max_results():
    client = _client(lambda req: httpx.Response(200, json=SAMPLE))
    t = make_web_search("http://searx", client=client)
    out = await t.call({"query": "x", "max_results": 2})
    await client.aclose()
    assert "First" in out and "Second" in out
    assert "Third" not in out


async def test_web_search_no_results():
    client = _client(lambda req: httpx.Response(200, json={"results": []}))
    t = make_web_search("http://searx", client=client)
    out = await t.call({"query": "nothing"})
    await client.aclose()
    assert out == "No results found."


async def test_web_search_tool_metadata():
    t = make_web_search("http://searx", client=_client(lambda req: httpx.Response(200, json={})))
    assert t.name == "web_search"
    params = t.spec()["function"]["parameters"]
    assert params["properties"]["query"]["type"] == "string"
    assert "query" in params["required"]
    assert "max_results" not in params["required"]  # has a default


async def test_web_search_http_error_returns_string_via_registry():
    # HTTP failures must surface as an error string (ToolRegistry never raises).
    client = _client(lambda req: httpx.Response(502, text="bad gateway"))
    t = make_web_search("http://searx", client=client)
    registry = ToolRegistry([t])
    out = await registry.invoke("web_search", {"query": "x"})
    await client.aclose()
    assert out.is_error
    assert "<tool_use_error>" in out.content
