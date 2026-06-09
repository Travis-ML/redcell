"""Tests for the in-memory session store (LRU + TTL eviction)."""

from redcell.sessions import SessionStore


class FakeClock:
    """A controllable monotonic clock for deterministic TTL tests."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_get_or_create_reuses_same_memory_per_id():
    store = SessionStore()
    a1 = store.get_or_create("a")
    a1.append({"role": "user", "content": "hi"})
    a2 = store.get_or_create("a")
    assert a2 is a1
    assert a2.load() == [{"role": "user", "content": "hi"}]


def test_distinct_ids_are_isolated():
    store = SessionStore()
    store.get_or_create("a").append({"role": "user", "content": "for-a"})
    assert store.get_or_create("b").load() == []
    assert len(store) == 2


def test_lru_eviction_at_max_sessions():
    store = SessionStore(max_sessions=2)
    store.get_or_create("a")
    store.get_or_create("b")
    store.get_or_create("a")  # touch 'a' so 'b' is now least-recently-used
    store.get_or_create("c")  # exceeds cap -> evicts 'b'
    assert len(store) == 2
    assert store.get_or_create("a").load() == []  # 'a' survived (was a fresh store)
    # 'b' was evicted -> a new empty memory is created on next access.
    assert store.get_or_create("b").load() == []


def test_ttl_eviction_drops_idle_sessions():
    clock = FakeClock()
    store = SessionStore(ttl_seconds=100.0, clock=clock)
    mem = store.get_or_create("a")
    mem.append({"role": "user", "content": "remember me"})

    clock.t = 50.0  # within ttl: still the same memory
    assert store.get_or_create("a") is mem

    clock.t = 50.0 + 101.0  # idle longer than ttl since last touch -> evicted
    fresh = store.get_or_create("a")
    assert fresh is not mem
    assert fresh.load() == []
