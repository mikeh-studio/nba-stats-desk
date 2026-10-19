"""Semantic calendar dependency; execution deadlines keep their real clocks."""

from contextvars import ContextVar
from datetime import date

semantic_date: ContextVar[date | None] = ContextVar("semantic_date", default=None)


def today() -> date:
    return semantic_date.get() or date.today()
