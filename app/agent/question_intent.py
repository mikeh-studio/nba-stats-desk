"""Recognize analytical shape before treating comparison words as identities.

These bounded guards are deterministic. They never infer missing facts, scoring
rules, trade dates, or lineup membership from a language model's world knowledge.
"""

from __future__ import annotations

import re


def has_specific_metric(question: str) -> bool:
    """Recognize metric changes before inheriting or broadening analysis intent."""
    return bool(
        re.search(
            r"\b(?:points?|rebounds?|assists?|steals?|blocks?|scoring|shooting|"
            r"minutes?|turnovers?|efficiency|field[ -]goals?|free[ -]throws?|"
            r"three[ -]pointers?|3[ -]pointers?|games? played|"
            r"pts|reb|ast|stl|blk|tov|fgm|fga|fg|ftm|fta|ft|3pm|3pa|3p|ts|efg)\b|%",
            question,
            re.I,
        )
    )


def split_kind(question: str) -> str | None:
    text = question.lower()
    compare = re.search(
        r"\b(?:vs\.?|versus|compare|compared|difference|gap|between)\b", text
    )
    if (
        compare
        and re.search(r"regular[ -]season", text)
        and re.search(r"\b(?:playoffs?|postseason)\b", text)
        and not re.search(r"\b(?:both|combined|including)\b", text)
    ):
        return "phase"
    if (
        compare
        and re.search(r"\bhome\b", text)
        and re.search(r"\b(?:away|road)\b", text)
    ):
        return "venue"
    if (
        compare
        and re.search(r"\blast\s+\d+\s+games?\b", text)
        and re.search(r"\b(?:prior|previous|preceding)\s+\d+\s+games?\b", text)
    ):
        return "recent_prior"
    if re.search(r"\bbefore\b", text) and re.search(r"\bafter\b", text):
        return "before_after"
    return None


def round_scope_message(question: str) -> str | None:
    """No round dimension exists: never pool a series request into a season."""
    if re.search(
        r"\b(?:finals|(?:nba|conference) final|the final(?!\s+(?:\d+|score|games?))|"
        r"championship series|semi[ -]?finals?|conference semis|"
        r"(?:first|second|third|1st|2nd|3rd|early|earlier|opening|final)[ -](?:playoff[ -])?rounds?|"
        r"round[ -](?:one|two|three|[1-4]))\b",
        question,
        re.I,
    ):
        return (
            "Finals and individual playoff rounds cannot be isolated with the connected data. "
            "I cannot compare that series with earlier rounds or the regular season reliably. "
            "I can summarize the full playoff run or regular season if you request that scope."
        )
    return None


def question_intent(question: str) -> dict[str, str | None]:
    """Return a route hint and, for unsupported grains, a precise next step."""
    kind = split_kind(question)
    if message := round_scope_message(question):
        return dict(
            kind="unavailable_round", status="unsupported_scope", message=message
        )
    if re.search(
        r"\b(?:lineups?|starting five|closing five|on[ -]court|off[ -]court|on/off|clutch|"
        r"possessions?|assisted baskets|deflections?|shot contests?)\b",
        question,
        re.I,
    ):
        return dict(
            kind="unavailable_grain",
            status="unsupported_scope",
            message=(
                "This needs possession, lineup or tracking data that is not connected. "
                "I can compare whole-game box scores with an explicit player and period; "
                "those cannot establish on/off, clutch or individual defensive impact."
            ),
        )
    if re.search(
        r"\b(?:salary|salaries|tax burden|cap space|contract|trade value)\b",
        question,
        re.I,
    ):
        return dict(
            kind="roster_finance",
            status="unsupported_scope",
            message=(
                "Contracts, salary-cap constraints and trade assets are not connected. "
                "A scoped production comparison is available, but cannot price a contract or validate a trade package."
            ),
        )
    if re.search(
        r"\b(?:fantasy|dynasty|keeper|punt|sell high|buy low|stash|drop|draft)\b",
        question,
        re.I,
    ) and re.search(
        r"\b(?:should|who|which|hold|drop|trade|draft|keep|target|value|sell|buy|stash)\b",
        question,
        re.I,
    ):
        return dict(
            kind="fantasy_decision",
            status="clarification_required",
            message=(
                "Please supply the exact scoring categories or point weights, league size, "
                "roster needs and available alternatives. I can compare observed category production; "
                "a hold/drop, trade or draft recommendation also needs those settings and future-role assumptions. "
                "The built-in fantasy proxies are not your league's scoring rules."
            ),
        )
    return dict(kind=kind or "statistics", status=None, message=None)
