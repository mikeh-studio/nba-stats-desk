"""Recognize analytical shape before treating comparison words as identities.

These bounded guards are deterministic. They never infer missing facts, scoring
rules, trade dates, or lineup membership from a language model's world knowledge.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from datetime import date
from typing import Any

from app.agent.catalog import load_semantic_catalog
from app.agent.clock import today as semantic_today
from app.agent.player_resolver import normalize_player_text
from app.agent.semantics import SemanticError, load_contract

_METRIC = re.compile(
    r"\b(?:points?|rebounds?|assists?|steals?|blocks?|scoring|shooting|"
    r"minutes?|turnovers?|efficiency|field[ -]goals?|free[ -]throws?|"
    r"three[ -]pointers?|3[ -]pointers?|games? played|"
    r"pts|reb|ast|stl|blk|tov|fgm|fga|fg|ftm|fta|ft|3pm|3pa|3p|ts|efg)\b|%",
    re.I,
)


def has_specific_metric(question: str) -> bool:
    """Recognize metric changes before inheriting or broadening analysis intent."""
    return bool(_METRIC.search(question))


def metric_terms(question: str) -> frozenset[str]:
    """Named metric words, normalized enough to compare two requests' tasks."""
    return frozenset(
        re.sub(r"[ -]", " ", term.lower()).rstrip("s")
        for term in _METRIC.findall(question)
    )


def split_kind(question: str) -> str | None:
    from app.agent.overview_positions import peer_question

    text = peer_question(question).lower()
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
            "Finals and individual playoff rounds cannot be isolated with the connected data, and I do not have a verified participation record for this request. "
            "This is a coverage limitation, not a claim that the player did not participate. "
            "I can summarize the full playoff run or regular season if you request that scope."
        )
    return None


def venue_mentions(question: str) -> tuple[bool, bool]:
    """Canonical literal home/road scope shared by metric and calendar guards."""
    return (
        bool(re.search(r"\bat home\b|\bhome games\b", question, re.I)),
        bool(re.search(r"\bon the road\b|\b(?:away|road) games\b", question, re.I)),
    )


def explicit_aggregation(question: str) -> str | None:
    """Recognized aggregation cues; unspecified language remains planner-owned."""
    # "Total rebounds" names the combined rebound statistic when followed by
    # a per-game rate; it is not a second requested aggregation.
    question = re.sub(
        r"\btotal\s+(?=rebounds?\s+(?:per[ -]game|a game)\b)", "", question, flags=re.I
    )
    total = bool(re.search(r"\b(?:total|totals|sum)\b", question, re.I))
    average = bool(
        re.search(
            r"\b(?:average|averaging|mean|ppg|per[ -]game|a game)\b", question, re.I
        )
    )
    if total and average:
        from app.agent.semantics import SemanticError

        raise SemanticError(
            "clarification_required", "Choose totals or per-game averages"
        )
    return "total" if total else "average" if average else None


def fantasy_decision(question):
    # Real-stat questions say "drop-off" or "drafted"; only fantasy wording or a
    # first-person roster move makes this a fantasy decision.
    fantasy = re.search(
        r"\b(?:fantasy|dynasty|keeper|punt|sell high|buy low|waivers?)\b",
        question,
        re.I,
    ) or re.search(
        r"\b(?:should\s+(?:i|we)|my\s+(?:team|roster|league))\b[^?]*"
        r"\b(?:drop(?![- ]?offs?\b)|stash|draft|pick\s+up|hold)\b",
        question,
        re.I,
    )
    if fantasy and re.search(
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
    return None


def question_scope(question: str) -> dict[str, str | None]:
    """Enforce representable scope before dispatch; no production route selection."""
    if decision := fantasy_decision(question):
        return decision
    kind = split_kind(question)
    if re.search(
        r"\b(?:not|excluding|except)\s+(?:the\s+)?(?:total(?:s)?|sum|average|mean|"
        r"per[ -]game|playoffs?|postseason|regular[ -]season)\b",
        question,
        re.I,
    ):
        return dict(
            kind="negated_scope",
            status="clarification_required",
            message="Please state the intended aggregation and season phase affirmatively.",
        )
    if re.search(
        r"\bhow many (?:points|pts)\b", question, re.I
    ) and not explicit_aggregation(question):
        return dict(
            kind="quantity_aggregation",
            status="clarification_required",
            message="Do you mean total points or points per game?",
        )
    if re.search(
        r"\b(?:causal|causation|cause[ds]?|predict\w*|forecast\w*|rolling|cumulative|"
        r"overtime|in wins|in losses|on (?:Mondays?|Tuesdays?|Wednesdays?|Thursdays?|Fridays?|Saturdays?|Sundays?)|"
        r"with (?:at least|more than|fewer than) \d+ (?:points|rebounds|assists))\b",
        question,
        re.I,
    ):
        return dict(
            kind="unsupported_calculation",
            status="unsupported_scope",
            message="This prediction, causal calculation or additional game filter is not supported by the connected statistics.",
        )
    if message := round_scope_message(question):
        return dict(
            kind="unavailable_round", status="unsupported_scope", message=message
        )
    if re.search(
        r"\b(?:lineups?|starting five|closing five|on[ -]court|off[ -]court|on/off|clutch|"
        r"possessions?|assisted baskets|deflections?|shot contests?|"
        r"offensive rebounds?|defensive rebounds?|oreb|dreb|box[ -]?outs?|"
        r"screen assists?|potential assists?|secondary assists?|charges drawn|"
        r"loose[ -]balls?|fouls?|rim protection|rim shots?|defended shots?|"
        r"defensive rating|rebound chances?|contested rebounds?)\b",
        question,
        re.I,
    ):
        return dict(
            kind="unavailable_grain",
            status="unsupported_scope",
            message=(
                "This needs rebound splits, possession, lineup, foul or tracking data that is not connected. "
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
    return dict(kind=kind or "statistics", status=None, message=None)


_REPLY_LEAD = re.compile(
    r"^\s*(?:(?:please|ok(?:ay)?|so|then|now|also|actually)\b[,\s]*|"
    r"(?:can|could|would)\s+you\s+(?:please\s+)?|"
    r"i\s+(?:want|need|would\s+like)\s+(?:to\s+(?:know|see)\s+)?)+",
    re.I,
)
_REQUEST_CUE = re.compile(
    r"^(?:how|what|who|which|when|where|why|is|are|was|were|did|does|do|has|have|"
    r"show|list|give|tell|compare|rank|find|get|pull|display|break\s+down|"
    r"summari[sz]e|analy[sz]e)\b",
    re.I,
)
# Reply fragments lean on the pending question for their subject or scope.
_REPLY_FRAGMENT = re.compile(
    r"^(?:(?:what|how)\s+about|what\s+if|which\s+(?:one|player|season|year|team)\b|"
    r"compare\s+(?:with|to|against)\b|(?:is|was)\s+it\b|"
    r"(?:show|give|tell)\s+me\s+(?:the\s+)?(?:regular|playoffs?|postseason|season|"
    r"first|second|last|other|one)\b)",
    re.I,
)
# "Show me Avery Example" answers "Which player?"; it asks for no task of its own.
_NAME_REPLY_LEAD = re.compile(
    r"^(?:(?:show|give|tell)\s+me(?:\s+about)?|show|give|pick|use|go\s+with|"
    r"i\s+mean[t]?|it'?s|(?:the\s+)?player)\b[,:\s]*",
    re.I,
)
_TASK_WORD = re.compile(
    r"\b(?:stats?|statistics|numbers|log|overview|profile|summary|leaders?|leads?|"
    r"season|playoffs?|postseason|games?|vs\.?|versus|compare|career|trend|chart|"
    r"table|similar|average|totals?)\b|\d",
    re.I,
)
_ANAPHORA = re.compile(
    r"\b(?:he|him|his|she|her|hers|they|them|their|it|same|instead|"
    r"that\s+(?:player|one|guy)|this\s+(?:player|one|guy))\b",
    re.I,
)


_TASK_MARKER = re.compile(
    r"\bgame[ -]?logs?\b|\bbox[ -]?scores?\b|\b(?:leaders?|leads?|leading|top|best|"
    r"rank(?:ed|ing|s)?|most|fewest|highest|lowest)\b|\bcareer\b|\btotals?\b",
    re.I,
)


def task_terms(question: str) -> frozenset[str]:
    """Metric and task words that define what a request asks for, not its scope."""
    markers = (
        re.sub(r"[ -]", "", m.lower()).rstrip("s")
        for m in _TASK_MARKER.findall(question)
    )
    return metric_terms(question) | frozenset(markers)


def name_reply(question: str) -> bool:
    """A reply that only names a player, with or without "show me"/"I meant"."""
    text = _NAME_REPLY_LEAD.sub("", _REPLY_LEAD.sub("", question).strip())
    text = text.strip(" .!?")
    return bool(
        0 < len(text.split()) <= 4
        and not _REQUEST_CUE.match(text)
        and not has_specific_metric(text)
        and not _TASK_WORD.search(text)
    )


_NOT_A_NAME = frozenset(
    """a an and the i me my show give tell pick use go with mean meant it its
    player please yes no ok okay both either neither all one first second third
    last other how what who which when where why is are was were did does do has
    have regular season seasons playoffs playoff postseason home away road
    january february march april may june july august september october november
    december monday tuesday wednesday thursday friday saturday sunday""".split()
)


def _fold(word: str) -> str:
    word = re.sub(r"['’]s$", "", word.strip(".,!?()'’-"))
    decomposed = unicodedata.normalize("NFKD", word.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def name_tokens(text: str) -> frozenset[str]:
    """Capitalized words that can belong to a player's name.

    Abbreviations (ATL), IDs and common reply or calendar words are excluded, so
    "Show me ATL" or "Both" names no one.
    """
    words = re.findall(r"[^\W\d_][\w'’.-]*", text)
    folded = (
        _fold(w) for w in words if w[0].isupper() and not w.isupper() and len(w) > 1
    )
    return frozenset(w for w in folded if w and w not in _NOT_A_NAME)


def names_other_player(reply: str, known_names: Iterable[str]) -> bool:
    """True when a reply names someone but matches none of the known names.

    ``known_names`` are the clarification's identity candidates or, when none
    were stored, the names in the pending question. Partial names ("Avery" for
    "Avery Example") match; with nothing named on either side the reply is not
    treated as naming someone else.
    """
    said = name_tokens(reply)
    known = {_fold(w) for name in known_names for w in name.split()} - {""}
    if not said or not known:
        return False
    return not any(
        s == k or (min(len(s), len(k)) >= 3 and (s in k or k in s))
        for s in said
        for k in known
    )


def starts_new_question(question: str) -> bool:
    """Recognize a self-contained request that may supersede a pending one.

    A request cue with at least three words and no reference back to the pending
    question (pronouns, "what about", "which one") stands on its own. Name
    replies such as "Show me Avery Example", scope fragments and thresholds such
    as "At least 1 FGA" do not. Whether it is a different task from the pending
    question is decided by ``routes.supersedes_pending``.
    """
    text = _REPLY_LEAD.sub("", question).strip()
    return bool(
        len(text.split()) >= 3
        and _REQUEST_CUE.match(text)
        and not _REPLY_FRAGMENT.match(text)
        and not _ANAPHORA.search(text)
        and not name_reply(text)
    )


def explicit_scope(question: str) -> dict[str, Any]:
    """Extract unambiguous literal scope; leave inferred language to the planner."""
    hints: dict[str, Any] = {}

    home, away = venue_mentions(question)
    if home != away:
        hints["home_away"] = "HOME" if home else "AWAY"
    from app.agent.overview_calendar import literal_date_intervals

    intervals = literal_date_intervals(question)
    if intervals:
        start, end = intervals[0]
        hints.update(window="date_range", start_date=start, as_of=end, n=None)
    if "window" not in hints:
        from app.agent.overview_calendar import MONTH_PATTERN, calendar_window

        has_calendar = re.search(
            MONTH_PATTERN
            + r"|\b(?:this|last|previous|past) (?:month|week)\b|\bweek (?:of|starting|beginning)\b",
            question,
            re.I,
        )
        # Complex calendar comparisons/ranges retain their established planner path.
        complex_calendar = len(re.findall(MONTH_PATTERN, question, re.I)) > 1 or bool(
            re.search(
                r"\b(?:since|before|after|until|through|between|versus|vs|compare)\b",
                question,
                re.I,
            )
        )
        standalone_calendar = bool(has_calendar) and not complex_calendar
        anchor = semantic_today()
        as_of = re.search(r"\bas[ -]of\s+(\d{4}-\d{2}-\d{2})\b", question, re.I)
        if standalone_calendar and as_of:
            try:
                anchor = date.fromisoformat(as_of[1])
            except ValueError as exc:
                raise SemanticError(
                    "clarification_required", "Please provide a valid as-of date."
                ) from exc
            if anchor > semantic_today():
                raise SemanticError(
                    "clarification_required",
                    "Please choose an as-of date no later than today.",
                )
        calendar_scope = (
            calendar_window(question, anchor) if standalone_calendar else None
        )
        if calendar_scope:
            start, end, _ = calendar_scope
            if start > anchor:
                raise SemanticError(
                    "clarification_required",
                    "Please choose a period starting no later than today.",
                )
            hints.update(
                window="date_range",
                start_date=start.isoformat(),
                as_of=min(end, anchor).isoformat(),
                n=None,
            )
    if (
        re.search(r"\b(?:both|combine|combined|including)\b", question, re.I)
        and re.search(r"regular[ -]season", question, re.I)
        and re.search(r"playoffs?|postseason", question, re.I)
    ):
        hints["season_type"] = "Both"
    days = re.search(
        r"\b(?:last|preceding)\s+(\d+)\s+(?:calendar\s+)?days\b", question, re.I
    )
    if days and "window" not in hints:
        hints.update(window="last_n_days", n=int(days.group(1)))
    months = re.search(r"\b(?:past|last|preceding)\s+(\d+)\s+months?\b", question, re.I)
    if months and "window" not in hints:
        hints.update(window="last_n_months", n=int(months.group(1)), start_date=None)
    return hints


def explicit_metric_mentions(question: str) -> set[str]:
    """Canonical aliases, excluding shorter aliases inside a longer match."""
    q = normalize_player_text(question)
    matches: list[tuple[int, int, str]] = []
    for metric in load_semantic_catalog().list_metrics():
        for name in [metric["key"], metric["label"], *metric.get("aliases", [])]:
            alias = normalize_player_text(str(name))
            if alias:
                matches.extend(
                    (
                        m.start(),
                        m.end(),
                        load_contract().default_fantasy_metric
                        if alias
                        in {
                            "fantasy",
                            "fantasy points",
                            "fantasy score",
                            "fantasy scoring",
                        }
                        else str(metric["key"]),
                    )
                    for m in re.finditer(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", q)
                )
    return {
        key
        for start, end, key in matches
        if not any(
            a <= start and end <= b and (a < start or end < b) for a, b, _ in matches
        )
    }


def comparison_scope_conflict(question, scope):
    """A shared literal date/window cannot erase distinct comparison sides."""
    from app.agent.overview_calendar import literal_date_intervals

    ranges = literal_date_intervals(question)
    days = re.findall(
        r"\b(?:last|preceding)\s+(\d+)\s+(?:calendar\s+)?days\b", question, re.I
    )
    windows = re.findall(
        r"\b(?:past|last|preceding|prior|previous)\s+(\d+)\s+(games?|days?|months?)\b",
        question,
        re.I,
    )
    return "window" in scope and (
        len(set(ranges)) > 1
        or bool(ranges and windows)
        or len(set(days)) > 1
        or len({(n, unit.lower().rstrip("s")) for n, unit in windows}) > 1
        or bool(
            re.search(
                r"\b(?:prior|previous)\s+\d+\s+(?:games|days|months?)\b|\bseason\s+(?:baseline|average|to date)\b|\bseason_to_date\b",
                question,
                re.I,
            )
        )
    )
