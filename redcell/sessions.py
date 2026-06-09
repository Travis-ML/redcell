"""Server-side conversation sessions: a bounded, expiring map of id -> Memory.

The HTTP server is stateless by default (the client resends history every turn).
When a request carries a session id, the server instead keys the conversation
history off that id so the client can send only the *new* turn. This is the
stateful target mode that tools like promptfoo's multi-turn red-team strategies
exercise.

State is held in memory only — sessions are lost on restart, which is fine for a
local red-team target. Growth is bounded two ways: idle sessions expire after
``ttl_seconds``, and the oldest session is evicted once ``max_sessions`` is hit.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from .memory import InMemoryStore, Memory


@dataclass
class _Entry:
    memory: Memory
    last_seen: float


@dataclass
class SessionStore:
    """An LRU+TTL map from session id to its :class:`Memory`.

    Args:
        max_sessions: hard cap on concurrent sessions; the least-recently-used is
            evicted when a new id would exceed it.
        ttl_seconds: idle lifetime — a session untouched for this long is dropped.
        clock: monotonic time source, injectable for tests.

    Note: ``get_or_create`` is synchronous and runs to completion without
    awaiting, so concurrent requests can't interleave inside it. We do assume a
    single conversation is driven sequentially (true for promptfoo's multi-turn
    strategies); overlapping requests on the *same* id would race on the returned
    ``Memory``.
    """

    max_sessions: int = 1000
    ttl_seconds: float = 3600.0
    clock: Callable[[], float] = time.monotonic
    _entries: OrderedDict[str, _Entry] = field(default_factory=OrderedDict, init=False)

    def get_or_create(self, session_id: str) -> Memory:
        """Return the :class:`Memory` for ``session_id``, creating it if needed."""
        now = self.clock()
        self._evict_expired(now)
        entry = self._entries.get(session_id)
        if entry is None:
            entry = _Entry(memory=InMemoryStore(), last_seen=now)
            self._entries[session_id] = entry
            self._evict_overflow()
        else:
            entry.last_seen = now
            self._entries.move_to_end(session_id)  # mark as most-recently-used
        return entry.memory

    def __len__(self) -> int:
        return len(self._entries)

    def _evict_expired(self, now: float) -> None:
        expired = [sid for sid, e in self._entries.items() if now - e.last_seen > self.ttl_seconds]
        for sid in expired:
            del self._entries[sid]

    def _evict_overflow(self) -> None:
        while len(self._entries) > self.max_sessions:
            self._entries.popitem(last=False)  # drop least-recently-used
