from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Protocol


@dataclass(frozen=True)
class ConversationTurn:
    question: str
    answer: str


class ConversationStore(Protocol):
    def get_turns(
        self, conversation_id: str, max_turns: int
    ) -> list[ConversationTurn]: ...

    def append_turn(
        self,
        conversation_id: str,
        *,
        question: str,
        answer: str,
        max_turns: int,
    ) -> None: ...


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._turns: dict[str, list[ConversationTurn]] = {}

    def get_turns(self, conversation_id: str, max_turns: int) -> list[ConversationTurn]:
        if max_turns <= 0:
            return []
        with self._lock:
            turns = list(self._turns.get(conversation_id, []))
        return turns[-max_turns:]

    def append_turn(
        self,
        conversation_id: str,
        *,
        question: str,
        answer: str,
        max_turns: int,
    ) -> None:
        if not conversation_id or max_turns <= 0:
            return
        with self._lock:
            turns = self._turns.setdefault(conversation_id, [])
            turns.append(ConversationTurn(question=question, answer=answer))
            del turns[:-max_turns]


_store = InMemoryConversationStore()


def get_conversation_store() -> ConversationStore:
    return _store
