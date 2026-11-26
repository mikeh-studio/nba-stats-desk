"""Semantic calendar dependency; execution deadlines keep their real clocks."""

from contextvars import ContextVar
from datetime import date, datetime
from zoneinfo import ZoneInfo

semantic_date: ContextVar[date | None] = ContextVar("semantic_date", default=None)


def today() -> date:
    return semantic_date.get() or date.today()


def today_in(timezone: str) -> date:
    return semantic_date.get() or datetime.now(ZoneInfo(timezone)).date()
