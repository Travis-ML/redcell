"""Offline tests for the RAG corpus loader and seeder."""

import json

import pytest

from redcell.rag.corpus import Doc, default_corpus_path, load_corpus


def test_bundled_corpus_file_exists():
    assert default_corpus_path().is_file()


def test_load_bundled_corpus_parses_and_splits():
    docs = load_corpus(default_corpus_path())
    assert len(docs) == 4
    assert all(isinstance(d, Doc) for d in docs)
    poison = [d for d in docs if d.poisoned]
    benign = [d for d in docs if not d.poisoned]
    assert poison and benign
    assert all(d.canary for d in poison)
    assert all(d.canary is None for d in benign)


def test_load_corpus_rejects_malformed_doc(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps([{"text": "missing id"}]))
    with pytest.raises(ValueError, match="malformed"):
        load_corpus(p)


def test_load_corpus_rejects_duplicate_canary(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(
        json.dumps(
            [
                {"id": "a", "text": "x", "poisoned": True, "canary": "DUP"},
                {"id": "b", "text": "y", "poisoned": True, "canary": "DUP"},
            ]
        )
    )
    with pytest.raises(ValueError, match="canary"):
        load_corpus(p)


def test_load_corpus_rejects_duplicate_id(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(
        json.dumps(
            [
                {"id": "same", "text": "x", "poisoned": False, "canary": None},
                {"id": "same", "text": "y", "poisoned": False, "canary": None},
            ]
        )
    )
    with pytest.raises(ValueError, match="id"):
        load_corpus(p)
