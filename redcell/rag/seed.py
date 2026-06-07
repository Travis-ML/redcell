"""Seed a corpus into the RAG store via the gateway's qdrant-store tool.

Storing through the MCP tool (rather than a direct Qdrant client) guarantees the
embeddings and collection schema match what ``qdrant-find`` retrieves.
"""

from __future__ import annotations

from ..tools import Tool
from .corpus import Doc


def build_store_args(doc: Doc) -> dict:
    """Map a Doc to qdrant-store arguments (information + metadata)."""
    return {
        "information": doc.text,
        "metadata": {"id": doc.id, "poisoned": doc.poisoned, "canary": doc.canary},
    }


def find_store_tool(tools: list[Tool]) -> Tool | None:
    """Find the gateway-exposed qdrant-store tool (namespaced, e.g. rag_qdrant-store)."""
    for t in tools:
        if "qdrant-store" in t.name.lower():
            return t
    return None


async def seed(tools: list[Tool], docs: list[Doc]) -> int:
    """Store each doc via the qdrant-store tool. Returns the count stored."""
    store = find_store_tool(tools)
    if store is None:
        raise RuntimeError(
            "no qdrant-store tool found via the gateway — is the 'rag' target up "
            "(redcell serve + Qdrant running)?"
        )
    for doc in docs:
        await store.call(build_store_args(doc))
    return len(docs)
