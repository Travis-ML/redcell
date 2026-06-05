from redcell.memory import InMemoryStore


def test_append_and_load_roundtrip():
    m = InMemoryStore()
    m.append({"role": "user", "content": "hi"})
    m.append({"role": "assistant", "content": "hello"})
    assert m.load() == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_load_returns_copy():
    m = InMemoryStore()
    m.append({"role": "user", "content": "hi"})
    snapshot = m.load()
    snapshot.append({"role": "user", "content": "mutation"})
    assert len(m.load()) == 1  # internal state not mutated
