"""redcell RAG: knowledge-base corpus loading and seeding."""

from .corpus import Doc, default_corpus_path, load_corpus

__all__ = ["Doc", "default_corpus_path", "load_corpus"]
