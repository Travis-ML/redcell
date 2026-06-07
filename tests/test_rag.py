"""Offline tests for the RAG corpus loader and seeder."""

import json

import pytest

from redcell.rag.corpus import Doc, default_corpus_path, load_corpus
from redcell.rag.seed import build_store_args, find_store_tool, seed
from redcell.tools import Tool


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


def _recording_store():
    calls = []

    async def qdrant_store(**kwargs):
        calls.append(kwargs)
        return "ok"

    qdrant_store.__name__ = "qdrant-store"
    tool = Tool(
        qdrant_store,
        schema={"type": "object", "properties": {}, "required": []},
        name="rag_qdrant-store",
        description="store",
    )
    return tool, calls


def test_build_store_args_shape():
    d = Doc(id="x1", text="hello", poisoned=True, canary="RC-CANARY-1")
    args = build_store_args(d)
    assert args["information"] == "hello"
    assert args["metadata"] == {"id": "x1", "poisoned": True, "canary": "RC-CANARY-1"}


def test_find_store_tool_picks_the_store():
    store, _ = _recording_store()

    async def find(**kw):
        return "[]"

    find.__name__ = "qdrant-find"
    find_tool = Tool(
        find, schema={"type": "object", "properties": {}, "required": []}, name="rag_qdrant-find"
    )
    assert find_store_tool([find_tool, store]) is store
    assert find_store_tool([store, find_tool]) is store
    assert find_store_tool([find_tool]) is None


async def test_seed_calls_store_once_per_doc():
    store, calls = _recording_store()
    docs = [
        Doc(id="a", text="t1", poisoned=False, canary=None),
        Doc(id="b", text="t2", poisoned=True, canary="RC-CANARY-2"),
    ]
    n = await seed([store], docs)
    assert n == 2
    assert [c["information"] for c in calls] == ["t1", "t2"]
    assert calls[1]["metadata"]["canary"] == "RC-CANARY-2"


async def test_seed_raises_without_store_tool():
    with pytest.raises(RuntimeError, match="qdrant-store"):
        await seed([], [Doc(id="a", text="t", poisoned=False, canary=None)])
