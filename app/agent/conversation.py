from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Protocol


@dataclass(frozen=True)
class ConversationTurn:
    question: str
    answer: str


@dataclass(frozen=True)
class PendingClarification:
    """The question (and plan) we still owe an answer for.

    Stored when the agent asks the user to clarify a player so the follow-up
    reply or option click can resume the original question without re-typing,
    re-planning, or re-resolving.
    """

    question: str
    query_plan: dict[str, Any] | None = None


@dataclass
class _ConversationState:
    turns: list[ConversationTurn] = field(default_factory=list)
    pending: PendingClarification | None = None


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

    def get_pending_clarification(
        self, conversation_id: str
    ) -> PendingClarification | None: ...

    def set_pending_clarification(
        self,
        conversation_id: str,
        *,
        question: str,
        query_plan: dict[str, Any] | None,
    ) -> None: ...

    def clear_pending_clarification(self, conversation_id: str) -> None: ...


class InMemoryConversationStore:
    def __init__(self, *, max_conversations: int = 1000) -> None:
        self._lock = Lock()
        self._max_conversations = max(1, max_conversations)
        self._conversations: dict[str, _ConversationState] = {}

    def _touch_locked(self, conversation_id: str) -> _ConversationState:
        # Every anonymous question creates a fresh conversation id, so the
        # store must stay bounded: keep the most recently used conversations
        # and evict the least recently used past the cap.
        state = self._conversations.pop(conversation_id, None)
        if state is None:
            state = _ConversationState()
        self._conversations[conversation_id] = state
        while len(self._conversations) > self._max_conversations:
            oldest_id = next(iter(self._conversations))
            del self._conversations[oldest_id]
        return state

    def get_turns(self, conversation_id: str, max_turns: int) -> list[ConversationTurn]:
        if max_turns <= 0:
            return []
        with self._lock:
            state = self._conversations.get(conversation_id)
            turns = list(state.turns) if state is not None else []
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
            state = self._touch_locked(conversation_id)
            state.turns.append(ConversationTurn(question=question, answer=answer))
            del state.turns[:-max_turns]

    def get_pending_clarification(
        self, conversation_id: str
    ) -> PendingClarification | None:
        if not conversation_id:
            return None
        with self._lock:
            state = self._conversations.get(conversation_id)
            return state.pending if state is not None else None

    def set_pending_clarification(
        self,
        conversation_id: str,
        *,
        question: str,
        query_plan: dict[str, Any] | None,
    ) -> None:
        if not conversation_id:
            return
        with self._lock:
            state = self._touch_locked(conversation_id)
            state.pending = PendingClarification(
                question=question, query_plan=query_plan
            )

    def clear_pending_clarification(self, conversation_id: str) -> None:
        if not conversation_id:
            return
        with self._lock:
            state = self._conversations.get(conversation_id)
            if state is not None:
                state.pending = None


_store = InMemoryConversationStore()


def get_conversation_store() -> ConversationStore:
    return _store
