"""Load the RAG seed corpus (benign + planted poison docs)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Doc:
    """One corpus document. Poison docs carry a unique canary marker."""

    id: str
    text: str
    poisoned: bool
    canary: str | None


def default_corpus_path() -> Path:
    """Path to the bundled seed corpus shipped with redcell."""
    return Path(__file__).parent / "seed_corpus.json"


def load_corpus(path: Path) -> list[Doc]:
    """Parse a corpus JSON file into validated ``Doc`` objects.

    Raises ``ValueError`` on a malformed file/entry, duplicate ids, or duplicate
    (non-null) canaries.
    """
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, list):
        raise ValueError("corpus must be a JSON list of documents")
    docs: list[Doc] = []
    for i, d in enumerate(raw):
        try:
            docs.append(
                Doc(
                    id=str(d["id"]),
                    text=str(d["text"]),
                    poisoned=bool(d.get("poisoned", False)),
                    canary=(d.get("canary") or None),
                )
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"doc[{i}] is malformed: {exc}") from exc
    ids = [d.id for d in docs]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate document id in corpus")
    canaries = [d.canary for d in docs if d.canary]
    if len(canaries) != len(set(canaries)):
        raise ValueError("duplicate canary in corpus")
    return docs
