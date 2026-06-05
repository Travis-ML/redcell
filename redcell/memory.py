"""Conversation memory: an abstract interface and an in-memory default."""

from __future__ import annotations

from abc import ABC, abstractmethod

Message = dict[str, object]


class Memory(ABC):
    """Stores the running message history for a conversation.

    The interface is intentionally small so alternative backends (Redis, a
    database, a summarizing window) can be dropped in without touching the
    agent loop.
    """

    @abstractmethod
    def append(self, message: Message) -> None:
        """Add one message to the history."""

    @abstractmethod
    def load(self) -> list[Message]:
        """Return the full history as a new list (callers may mutate it)."""

    @abstractmethod
    def clear(self) -> None:
        """Drop all stored messages."""


class InMemoryStore(Memory):
    """Default :class:`Memory` backend holding messages in a Python list."""

    def __init__(self) -> None:
        self._messages: list[Message] = []

    def append(self, message: Message) -> None:
        self._messages.append(message)

    def load(self) -> list[Message]:
        return list(self._messages)

    def clear(self) -> None:
        self._messages.clear()
