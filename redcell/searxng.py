"""A `web_search` agent tool backed by a SearXNG instance.

SearXNG is a self-hosted metasearch engine. With JSON output enabled it answers
``GET <base>/search?q=...&format=json`` with ``results`` (and sometimes direct
``answers``). This module turns that into a single :class:`~redcell.tools.Tool`.
"""

from __future__ import annotations

import httpx

from .tools import Tool, tool

DEFAULT_SEARXNG_URL = "http://127.0.0.1:8989"
_TIMEOUT = 15.0


def _format(data: dict, max_results: int) -> str:
    """Render a SearXNG JSON payload as a compact, model-readable string."""
    lines: list[str] = []

    for answer in data.get("answers") or []:
        text = answer.get("answer") if isinstance(answer, dict) else answer
        if text:
            lines.append(f"Answer: {text}")

    results = (data.get("results") or [])[:max_results]
    if not results and not lines:
        return "No results found."

    for i, r in enumerate(results, 1):
        title = r.get("title") or "(no title)"
        url = r.get("url") or ""
        entry = f"{i}. {title} — {url}"
        content = (r.get("content") or "").strip()
        if content:
            entry += f"\n   {content}"
        lines.append(entry)

    return "\n".join(lines)


def make_web_search(
    base_url: str = DEFAULT_SEARXNG_URL,
    *,
    client: httpx.AsyncClient | None = None,
) -> Tool:
    """Build a ``web_search`` tool bound to a SearXNG instance.

    Args:
        base_url: root URL of the SearXNG instance (no trailing ``/search``).
        client: optional shared ``AsyncClient`` (used by tests to inject a mock
            transport). When omitted, a client is created per call.
    """
    root = base_url.rstrip("/")

    async def _query(http: httpx.AsyncClient, query: str, max_results: int) -> str:
        resp = await http.get(
            f"{root}/search",
            params={"q": query, "format": "json"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return _format(resp.json(), max_results)

    async def web_search(query: str, max_results: int = 5) -> str:
        """Search the web via SearXNG. Returns ranked results as title, URL, and snippet."""
        if client is not None:
            return await _query(client, query, max_results)
        async with httpx.AsyncClient() as http:
            return await _query(http, query, max_results)

    # A search is read-only and safe to run alongside other read-only tools.
    return tool(web_search, read_only=True, concurrency_safe=True)
