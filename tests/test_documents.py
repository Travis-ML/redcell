"""Tests for PDF document ingestion into the RAG store (no real PDFs/Qdrant)."""

from pathlib import Path

import redcell.rag.documents as docs
from redcell.rag.documents import (
    Chunk,
    build_chunk_args,
    chunk_text,
    discover_pdfs,
    ingest_documents,
    load_manifest,
    save_manifest,
)
from redcell.tools import Tool

# --- chunking -------------------------------------------------------------


def test_chunk_text_short_is_single_chunk():
    assert chunk_text("hello world", size=100, overlap=10) == ["hello world"]


def test_chunk_text_empty():
    assert chunk_text("   \n\t ", size=100, overlap=10) == []


def test_chunk_text_splits_with_overlap():
    text = "abcdefghij" * 30  # 300 chars
    chunks = chunk_text(text, size=100, overlap=20)
    assert len(chunks) > 1
    # Each chunk is at most `size`, and consecutive chunks overlap.
    assert all(len(c) <= 100 for c in chunks)
    assert chunks[0][-20:] == chunks[1][:20]
    # The whole text is covered (concatenating de-overlapped pieces).
    assert chunks[0][0] == "a" and text.endswith(chunks[-1][-1])


def test_chunk_text_overlap_ge_size_is_handled():
    # overlap >= size must not loop forever or crash.
    chunks = chunk_text("x" * 50, size=10, overlap=10)
    assert chunks and all(len(c) <= 10 for c in chunks)


# --- discovery & manifest -------------------------------------------------


def test_discover_pdfs_flat_only(tmp_path: Path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1")
    (tmp_path / "b.PDF").write_bytes(b"%PDF-1")
    (tmp_path / "c.txt").write_bytes(b"nope")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "deep.pdf").write_bytes(b"%PDF-1")  # must NOT be found (flat)

    found = [p.name for p in discover_pdfs(tmp_path)]
    assert found == ["a.pdf", "b.PDF"]  # sorted, flat, pdf-only


def test_discover_pdfs_missing_dir(tmp_path: Path):
    assert discover_pdfs(tmp_path / "nope") == []


def test_manifest_roundtrip(tmp_path: Path):
    p = tmp_path / "nested" / "ingested.json"
    assert load_manifest(p) == {}  # missing -> empty
    save_manifest(p, {"a": "h1"})
    assert load_manifest(p) == {"a": "h1"}


def test_build_chunk_args_shape():
    args = build_chunk_args(Chunk(source="r.pdf", page=2, index=3, text="body"))
    assert args["information"] == "body"
    assert args["metadata"] == {"source": "r.pdf", "page": 2, "chunk": 3, "kind": "document"}


# --- ingest ---------------------------------------------------------------


def _store_tool(sink: list[dict]) -> Tool:
    async def call(**kwargs):
        sink.append(kwargs)
        return "stored"

    call.__name__ = "qdrant-store"
    return Tool(call, schema={"type": "object", "properties": {}}, name="qdrant-store")


async def test_ingest_stores_chunks_then_skips_unchanged(tmp_path, monkeypatch):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-fake-bytes")
    monkeypatch.setattr(docs, "extract_pages", lambda path: ["alpha beta gamma " * 60])
    manifest = tmp_path / ".redcell/ingested.json"
    sink: list[dict] = []
    tools = [_store_tool(sink)]

    first = await ingest_documents(
        tools, tmp_path, manifest_path=manifest, chunk_size=100, chunk_overlap=20
    )
    assert first["files"] == 1
    assert first["chunks"] == len(sink) > 1
    assert manifest.exists()

    # Second run: same bytes -> skipped, no new store calls.
    before = len(sink)
    second = await ingest_documents(
        tools, tmp_path, manifest_path=manifest, chunk_size=100, chunk_overlap=20
    )
    assert second == {"files": 0, "chunks": 0, "skipped": 1}
    assert len(sink) == before


async def test_ingest_reingest_when_file_changes(tmp_path, monkeypatch):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"v1")
    monkeypatch.setattr(docs, "extract_pages", lambda path: ["some text here"])
    manifest = tmp_path / "m.json"
    sink: list[dict] = []
    tools = [_store_tool(sink)]

    await ingest_documents(tools, tmp_path, manifest_path=manifest)
    pdf.write_bytes(b"v2-different")  # content changed -> hash changes
    result = await ingest_documents(tools, tmp_path, manifest_path=manifest)
    assert result["files"] == 1  # re-ingested


async def test_ingest_no_store_tool_skips(tmp_path, monkeypatch):
    (tmp_path / "doc.pdf").write_bytes(b"x")
    monkeypatch.setattr(docs, "extract_pages", lambda path: ["text"])
    result = await ingest_documents([], tmp_path, manifest_path=tmp_path / "m.json")
    assert result == {"files": 0, "chunks": 0, "skipped": 1}


async def test_ingest_empty_folder(tmp_path):
    result = await ingest_documents([], tmp_path, manifest_path=tmp_path / "m.json")
    assert result == {"files": 0, "chunks": 0, "skipped": 0}
