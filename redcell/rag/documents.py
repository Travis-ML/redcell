"""Ingest PDFs from a folder into the RAG store at `serve` startup.

For each PDF it extracts page text (pypdf), splits it into overlapping chunks,
and stores each chunk via the gateway's ``qdrant-store`` tool — the same path as
:mod:`redcell.rag.seed`, so the embeddings match what ``qdrant-find`` retrieves.
A local hash **manifest** records each file's content hash, so a restart only
(re)ingests new or modified PDFs and skips unchanged ones.

Resilient by design: if the store tool isn't available (gateway/Qdrant down) or a
single store call fails, it logs and continues — startup never fails because of
ingestion.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ..tools import Tool
from .seed import find_store_tool

logger = logging.getLogger("redcell.rag.documents")


@dataclass(frozen=True)
class Chunk:
    """One stored unit: a slice of a document, with provenance for citation."""

    source: str  # file name
    page: int  # 1-based page number
    index: int  # chunk index within the document
    text: str


def discover_pdfs(folder: Path) -> list[Path]:
    """Top-level ``*.pdf`` files in ``folder`` (flat, no recursion), sorted."""
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")


def file_hash(path: Path) -> str:
    """SHA-256 of the file's bytes (the manifest dedup key)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract_pages(path: Path) -> list[str]:
    """Return the extracted text of each page via pypdf."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return [(page.extract_text() or "") for page in reader.pages]


def chunk_text(text: str, *, size: int, overlap: int) -> list[str]:
    """Split ``text`` into ~``size``-char chunks overlapping by ``overlap`` chars.

    Whitespace is normalized first. A sliding window with ``step = size - overlap``
    keeps adjacent chunks overlapping so a passage split across a boundary is still
    retrievable from at least one chunk.
    """
    text = " ".join(text.split())
    if not text:
        return []
    if overlap >= size:
        overlap = size // 4
    step = max(1, size - overlap)
    chunks: list[str] = []
    for start in range(0, len(text), step):
        piece = text[start : start + size].strip()
        if piece:
            chunks.append(piece)
        if start + size >= len(text):
            break
    return chunks


def chunk_pdf(path: Path, *, size: int, overlap: int) -> list[Chunk]:
    """Extract and chunk a PDF into :class:`Chunk`s (one stream of indices)."""
    chunks: list[Chunk] = []
    for pageno, page_text in enumerate(extract_pages(path), start=1):
        for piece in chunk_text(page_text, size=size, overlap=overlap):
            chunks.append(Chunk(source=path.name, page=pageno, index=len(chunks), text=piece))
    return chunks


def build_chunk_args(chunk: Chunk) -> dict:
    """Map a :class:`Chunk` to ``qdrant-store`` arguments (information + metadata)."""
    return {
        "information": chunk.text,
        "metadata": {
            "source": chunk.source,
            "page": chunk.page,
            "chunk": chunk.index,
            "kind": "document",
        },
    }


def load_manifest(path: Path) -> dict[str, str]:
    """Load the ingest manifest (file path -> content hash); {} if missing/bad."""
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_manifest(path: Path, manifest: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True))


async def ingest_documents(
    tools: list[Tool],
    folder: str | Path,
    *,
    manifest_path: str | Path,
    chunk_size: int = 1000,
    chunk_overlap: int = 150,
) -> dict:
    """Ingest new/changed PDFs in ``folder`` via ``qdrant-store``.

    Returns a summary dict ``{files, chunks, skipped}``. Unchanged files (same
    content hash as the manifest) are skipped; the manifest is updated per file so
    an interrupted run resumes without redoing finished files.
    """
    folder = Path(folder)
    pdfs = discover_pdfs(folder)
    if not pdfs:
        logger.info("documents: no PDFs found in %s", folder)
        return {"files": 0, "chunks": 0, "skipped": 0}

    store = find_store_tool(tools)
    if store is None:
        logger.warning(
            "documents: no qdrant-store tool (gateway/Qdrant up?); skipping %d file(s)",
            len(pdfs),
        )
        return {"files": 0, "chunks": 0, "skipped": len(pdfs)}

    manifest_path = Path(manifest_path)
    manifest = load_manifest(manifest_path)
    files = chunks_stored = skipped = 0

    for pdf in pdfs:
        digest = file_hash(pdf)
        key = str(pdf.resolve())
        if manifest.get(key) == digest:
            skipped += 1
            continue

        chunks = chunk_pdf(pdf, size=chunk_size, overlap=chunk_overlap)
        if not chunks:
            logger.warning(
                "documents: %s yielded no extractable text (scanned/needs OCR?)", pdf.name
            )

        stored = 0
        for chunk in chunks:
            result = await store.call(build_chunk_args(chunk))
            if isinstance(result, str) and result.startswith("Error:"):
                logger.warning(
                    "documents: store failed for %s chunk %d: %s", pdf.name, chunk.index, result
                )
                continue
            stored += 1

        manifest[key] = digest
        save_manifest(manifest_path, manifest)  # persist per file (resumable)
        files += 1
        chunks_stored += stored
        logger.info("documents: ingested %s (%d/%d chunks)", pdf.name, stored, len(chunks))

    logger.info(
        "documents: %d file(s) ingested, %d chunk(s) stored, %d unchanged",
        files,
        chunks_stored,
        skipped,
    )
    return {"files": files, "chunks": chunks_stored, "skipped": skipped}
