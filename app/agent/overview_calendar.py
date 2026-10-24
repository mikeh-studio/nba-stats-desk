"""Explicit calendar windows for player overviews; ambiguous dates never widen."""

import calendar
import re
from datetime import date, timedelta

from app.agent.semantics import SemanticError

MONTHS = {
    name.lower(): n
    for n in range(1, 13)
    for name in (calendar.month_name[n], calendar.month_abbr[n])
}
MONTHS["sept"] = 9
MONTH_PATTERN = r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?\b"


# Introductory prose does not define an interval: the date anchors and connector do.
ISO_DATE_PATTERN = r"\b\d{4}-\d{2}-\d{2}\b"
ISO_INTERVAL_PATTERN = (
    r"\b(\d{4}-\d{2}-\d{2})\s+(?:to|through)\s+(\d{4}-\d{2}-\d{2})\b"
    r"|\bbetween\s+(\d{4}-\d{2}-\d{2})\s+and\s+(\d{4}-\d{2}-\d{2})\b"
)


def literal_date_intervals(question: str) -> list[tuple[str, str]]:
    """Canonical ISO intervals, with real dates and order validated once."""
    intervals = []
    for match in re.finditer(ISO_INTERVAL_PATTERN, question, re.I):
        start, end = match.group(1, 2) if match[1] else match.group(3, 4)
        try:
            lo, hi = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError as exc:
            raise SemanticError("invalid_scope", "Invalid explicit date range") from exc
        if lo > hi:
            raise SemanticError("invalid_scope", "Date range is reversed")
        intervals.append((start, end))
    return intervals


def shift_months(day, n):
    year, month = divmod(day.year * 12 + day.month - 1 - n, 12)
    if year < 1 or year > 9999:
        raise SemanticError("invalid_scope", "Please choose a shorter month window.")
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def calendar_window(question, today):
    """Return start, end, calendar-month comparison flag, or no calendar expression.

    A dated 'week of' means seven inclusive days beginning on that date.
    This/last week use Monday boundaries. Relative singular past/last windows
    are bounded here; numeric rolling windows remain with overview_scope.
    """
    original, separator, reply = question.partition("\nClarification:")
    if separator:
        original_months = list(re.finditer(MONTH_PATTERN, original, re.I))
        year_reply = re.fullmatch(r"\s*(20\d{2})[.!]?\s*", reply)
        if year_reply and len(original_months) == 1:
            end = original_months[0].end()
            return calendar_window(
                original[:end] + " " + year_reply[1] + original[end:], today
            )
        if literal_date_intervals(reply):
            return None  # overview_scope handles the explicit clarification range.
        if re.search(MONTH_PATTERN, reply, re.I) or re.search(
            r"\b(?:months?|weeks?)\b", reply, re.I
        ):
            return calendar_window(reply, today)
    if (
        len(
            re.findall(
                r"\b(?:this|last|previous|past)\s+(?:month|week)\b|\b(?:past|last|preceding)\s+\d+\s+(?:months?|weeks?|days?)\b",
                question,
                re.I,
            )
        )
        > 1
    ):
        raise SemanticError(
            "clarification_required",
            "Please specify one calendar window or explicit start and end dates.",
        )
    months = list(re.finditer(MONTH_PATTERN, question, re.I))
    week = re.search(
        r"\bweek (?:of|starting|beginning)\s+(\d{4}-\d{2}-\d{2})\b", question, re.I
    )
    relative = re.search(
        r"\b(this|last|previous|past)\s+(month|week)\b", question, re.I
    )
    if len(months) > 1 or sum(bool(x) for x in (months, week, relative)) > 1:
        raise SemanticError(
            "clarification_required",
            "Please specify a single calendar window or explicit start and end dates.",
        )
    if months and re.search(
        r"\b(?:weeks?|since|before|after|until|through)\b", question, re.I
    ):
        raise SemanticError(
            "clarification_required",
            "Please use explicit start and end dates for a partial month or week within a month.",
        )
    if len(re.findall(r"\bweek (?:of|starting|beginning)\b", question, re.I)) > 1:
        raise SemanticError(
            "clarification_required",
            "Please specify one week or explicit start and end dates.",
        )
    if months:
        match = months[0]
        year = re.match(r"\.?\s+(20\d{2})\b(?![-/]\d)", question[match.end() :])
        if not year:
            raise SemanticError(
                "clarification_required",
                "Please include the year with the month, for example January 2026.",
            )
        start = date(int(year[1]), MONTHS[match[1].lower()], 1)
        end = date(
            start.year, start.month, calendar.monthrange(start.year, start.month)[1]
        )
        return start, end, True
    if week:
        try:
            start = date.fromisoformat(week[1])
        except ValueError as exc:
            raise SemanticError(
                "clarification_required", "Please provide a valid week start date."
            ) from exc
        return start, start + timedelta(days=6), False
    if relative:
        which, unit = relative[1].lower(), relative[2].lower()
        if unit == "week":
            monday = today - timedelta(days=today.weekday())
            if which == "this":
                return monday, today, False
            if which in ("last", "previous"):
                return monday - timedelta(days=7), monday - timedelta(days=1), False
            return today - timedelta(days=6), today, False
        if which == "this":
            return today.replace(day=1), today, True
        if which in ("last", "previous"):
            end = today.replace(day=1) - timedelta(days=1)
            return end.replace(day=1), end, True
        # Past month uses the same inclusive rolling rule as past 1 month.
        return shift_months(today, 1) + timedelta(days=1), today, False
    if re.search(r"\b(?:months?|weeks?)\b", question, re.I) and not re.search(
        r"\b(?:past|last|preceding)\s+\d+\s+(?:months?|weeks?|days?)\b", question, re.I
    ):
        raise SemanticError(
            "clarification_required",
            "Please specify a month and year, a week starting YYYY-MM-DD, or explicit start and end dates.",
        )
    return None


def since_month_start(question: str) -> str | None:
    """Recognize the existing open-ended since-month scope, not a closed interval.

    Additional date anchors cannot use this legacy path to bypass interval checks.
    """
    question = re.sub(r"^may i (?:see|have|get)\s+", "", question, flags=re.I)
    matches = list(
        re.finditer(r"\bsince\s+" + MONTH_PATTERN + r"\s+(20\d{2})\b", question, re.I)
    )
    if (
        len(matches) != 1
        or len(re.findall(MONTH_PATTERN, question, re.I)) != 1
        or re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b", question)
    ):
        return None
    match = matches[0]
    return date(int(match[2]), MONTHS[match[1].lower()], 1).isoformat()


def validate_single_period(question: str) -> None:
    """Reject competing literal calendar/phase/venue scopes for one summary."""
    text = re.sub(r"^may i (?:see|have|get)\s+", "", question, flags=re.I)
    intervals = literal_date_intervals(text)
    remaining = re.sub(ISO_INTERVAL_PATTERN, " ", text, flags=re.I)
    periods = re.findall(
        r"\bweek (?:of|starting|beginning)\s+\d{4}-\d{2}-\d{2}\b"
        r"|\b(?:this|last|previous|past) (?:month|week)\b|" + MONTH_PATTERN,
        remaining,
        re.I,
    )
    if len(intervals) + len(periods) > 1:
        raise SemanticError("clarification_required", "Provide one date period")
    if intervals and re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b", remaining):
        raise SemanticError(
            "clarification_required", "Extra date anchors require clarification"
        )
    validate_phase_venue(question)


def validate_phase_venue(question: str) -> None:
    """Shared literal phase/venue contradictions, independent of period count."""
    from app.agent.scope_policy import venue_mentions

    text = question
    regular = bool(re.search(r"\bregular[ -]season\b", text, re.I))
    playoff = bool(re.search(r"\b(?:playoffs?|postseason)\b", text, re.I))
    combined = bool(re.search(r"\b(?:both|combine|combined|including)\b", text, re.I))
    home, away = venue_mentions(text)
    if (regular and playoff and not combined) or (home and away):
        raise SemanticError(
            "clarification_required",
            "Provide one date period and nonconflicting phase and venue",
        )
