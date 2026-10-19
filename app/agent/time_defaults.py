"""Shared, deterministic conversational time defaults; evidence never changes intent."""

import calendar
import re
from datetime import date

from app.agent.clock import today as semantic_today
from app.agent.overview_calendar import MONTH_PATTERN, MONTHS
from app.agent.semantics import SemanticError
from app.seasons import SEASONS, season_bounds


def resolve_time_defaults(question, today=None, context=None):
    today = today or semantic_today()
    anchor = re.search(r"\bas[ -]of (\d{4}-\d{2}-\d{2})\b", question, re.I)
    if anchor:
        try:
            today = date.fromisoformat(anchor[1])
        except ValueError as exc:
            raise SemanticError(
                "clarification_required", "Please provide a valid as-of date."
            ) from exc
    context = context or {}
    original = question
    notes = []
    alternatives = []
    # Explicit ranges and clarification replies already have their own validators.
    if "\nClarification:" in question:
        return question, notes, alternatives
    patterns = [
        r"\b(?:last|previous|prior) (?:year|season)\b",
        r"\b(?:this|current) year\b",
        r"\b(?:past|preceding) year\b|\b(?:past|last|preceding) \d+ years?\b",
        r"\b(?:recently|lately)\b",
        r"\b(?:in|during|calendar year) (20\d{2})\b(?![-/–]\d)(?!\s+(?:playoffs?|postseason))",
    ]
    matches = [m for pattern in patterns for m in re.finditer(pattern, question, re.I)]
    if len(matches) > 1 or (
        matches
        and re.search(
            r"\bfrom \d{4}-\d{2}-\d{2}|\b(?:past|last) \d+ (?:months?|weeks?|days?)\b",
            question,
            re.I,
        )
    ):
        raise SemanticError(
            "clarification_required",
            "These periods conflict. Choose one period using the suggested dates.",
        )
    if matches:
        m = matches[0]
        phrase = m[0].lower()
        season_start = today.year - (today.month < 7)
        if re.match(r"(?:last|previous|prior) (?:year|season)", phrase):
            year = season_start - 1
            replacement = f"{year}-{(year + 1) % 100:02}"
            note = f"Using ‘{m[0]}’ as the last completed NBA season ({replacement})."
            base = original[: m.start()] + original[m.end() :]
            alternatives = [
                {
                    "label": f"Calendar year {today.year - 1}",
                    "question": base
                    + f" from {today.year - 1}-01-01 through {today.year - 1}-12-31",
                },
                {"label": "Past 12 months", "question": base + " past 12 months"},
            ]
        elif phrase.startswith(("this ", "current ")):
            replacement = f"{season_start}-{(season_start + 1) % 100:02}"
            note = f"Using ‘{m[0]}’ as NBA season {replacement}; coverage is reported separately."
        elif phrase in ("recently", "lately"):
            replacement = "last 10 games"
            note = f"Using ‘{m[0]}’ as the last 10 recorded appearances."
        elif "year" in phrase and not m.groups():
            n = int(re.search(r"\d+", phrase)[0]) if re.search(r"\d+", phrase) else 1
            replacement = f"past {n * 12} months"
            note = f"Using ‘{m[0]}’ as a rolling {n * 12}-month window."
        else:
            year = int(m[1])
            end = min(date(year, 12, 31), today)
            replacement = f"from {year}-01-01 through {end}"
            note = f"Using {year} as a calendar year ({year}-01-01 through {end})."
        question = question[: m.start()] + replacement + question[m.end() :]
        notes.append(note)
    months = list(re.finditer(MONTH_PATTERN, question, re.I))
    if len(months) == 1 and not re.search(
        r"\b(?:since|before|after|until|through|weeks?)\b", question, re.I
    ):
        m = months[0]
        if not re.match(r"\.?\s+20\d{2}\b", question[m.end() :]):
            month = MONTHS[m[1].lower()]
            explicit_seasons = {
                f"{m[1]}-{m[2][-2:]}"
                for m in re.finditer(
                    r"\b(20\d{2})[-–/](20\d{2}|\d{2})\b(?![-/–]\d)", question
                )
            }
            explicit_seasons.update(
                f"{int(m[1]) - 1}-{m[1][-2:]}"
                for m in re.finditer(
                    r"\b(20\d{2})\s+(?:playoffs?|postseason)\b", question, re.I
                )
            )
            if len(explicit_seasons) > 1:
                return question, notes, alternatives
            scope = context.get("scope") or {}
            seasons = scope.get("seasons") or []
            season = next(iter(explicit_seasons), None)
            if not season and len(seasons) <= 1:
                season = seasons[0] if seasons else scope.get("season")
            end = scope.get("end") or scope.get("as_of")
            start = scope.get("start") or scope.get("start_date")
            context_anchor = today
            try:
                if end:
                    context_anchor = min(today, date.fromisoformat(end))
                if not season and start and end:
                    lo, hi = date.fromisoformat(start), date.fromisoformat(end)
                    season = next(
                        (
                            s
                            for s in SEASONS
                            if season_bounds(s)[0] <= lo <= hi <= season_bounds(s)[1]
                        ),
                        None,
                    )
            except (ValueError, TypeError):
                pass  # Invalid legacy metadata cannot override the request anchor.
            year = (
                int(season[:4]) + (month < 7)
                if season
                else context_anchor.year - (month > context_anchor.month)
            )
            question = question[: m.end()] + f" {year}" + question[m.end() :]
            notes.append(
                f"Using {calendar.month_name[month]} {year} for the month without a year."
            )
    return question, notes, alternatives
