"""Opponent splits computed from governed player-game facts."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from functools import lru_cache
from math import isclose
from statistics import mean, pstdev
from typing import Any

from app.agent.award_lookup import wants_award
from app.agent.catalog import load_semantic_catalog
from app.agent.clock import today as semantic_today
from app.agent.game_filters import extract_game_filters
from app.agent.name_matching import (
    _SCOPE_WORDS,
    longest_player_spans,
    mask_player_tokens,
    prepare_names,
)
from app.agent.overview_calendar import (
    ISO_INTERVAL_PATTERN,
    MONTH_PATTERN,
    literal_date_intervals,
)
from app.agent.payload import answer_payload
from app.agent.player_comparison import comparison_sides
from app.agent.player_resolver import normalize_player_text
from app.agent.scope_policy import (
    _ROUND_PATTERN,
    COLLECTIVE_MATCHUP,
    explicit_metric_mentions,
    explicit_phase,
    explicit_scope,
    requested_playoff_round,
    round_scope_message,
    split_kind,
    venue_mentions,
)
from app.agent.semantic_serving import seasons_in_dates
from app.agent.semantics import (
    Evidence,
    Metric,
    Query,
    SemanticError,
    _game_result,
    aggregate,
    load_contract,
    select_query_rows,
)
from app.seasons import validate_season

VERSION = "opponent_splits/1"
DEFAULT_METRICS = ("pts", "ts_pct", "fg_pct", "fg3_pct", "ast", "reb", "plus_minus")
SCORE_METRICS = ("pts", "ts_pct", "plus_minus")
SMALL_SAMPLE = 3
_METRIC_REPLY = "\nOpponent split metrics: "
_OPPONENT_CUE = re.compile(
    r"\b(?:opponent[ -](?:splits?|breakdown)|(?:split|breakdown)s? (?:by|across) opponent"
    rf"|{COLLECTIVE_MATCHUP}"
    r"|by opponent|(?:toughest|hardest|easiest|best|worst)\s+(?:matchups?|opponents?)"
    r"|(?:which|what) (?:opponent|team)\b[^?\n]*\bstruggl\w*[^?\n]*\bagainst"
    r"|\bstruggl\w*\s+(?:the\s+)?(?:most\s+)?against which (?:opponent|team))\b",
    re.I,
)
_LAST_GAMES = re.compile(
    r"\b(?:last|past|most recent)\s+(\d+)\s+"
    r"(?:(home|away|road|playoffs?|regular[ -]season)\s+)?(?:games?|appearances?)\b",
    re.I,
)
_AWAY_FROM_HOME = re.compile(r"\baway[- ]from[- ]home\b", re.I)
_VENUE_ONLY_OR_NOUN = re.compile(
    r"\b(?:(?P<home>home)|(?P<away>away|road))\s+"
    r"(?:only|appearances?|matchups?|contests?|splits?)\b"
    r"|\bonly\s+(?:(?P<only_home>home)|(?P<only_away>away|road))\b",
    re.I,
)
_RESOLVED_VENUE = re.compile(
    r"\bat\s+home\b|\bon\s+the\s+road\b"
    r"|\b(?:in\s+)?(?:home|away|road)\s+games\b|\bgames\s+away\b",
    re.I,
)
_UNRESOLVED_VENUE = re.compile(
    r"\b(?:home|away|road|neutral(?:[- ]site)?)\b",
    re.I,
)
_SLASH_DATE = re.compile(r"\b(\d{4})/(\d{2})/(\d{2})\b")
_UNRESOLVED_RANGE = re.compile(
    r"\bfrom\s+(?:(?!(?:\bfrom\b|\bbetween\b|[?!.])).)+?\s+"
    r"(?:through|until|to(?=\s+(?!(?:see|find|check|know|determine|evaluate"
    r"|understand|measure|identify|assess|compare|show|get|answer)\b)))\b"
    r"|\bbetween\s+(?:(?!(?:\bfrom\b|\bbetween\b|[?!.])).)+?\s+and\b",
    re.I,
)
_MULTI_SEASON = re.compile(
    r"\b(?:(?:last|past|previous|prior|preceding|most recent|next)\s+(?:[\w-]+\s+){0,2}seasons"
    r"|(?:multiple|several|many|both|all|every|each|various|different|consecutive|combined)\s+seasons"
    r"|(?:across|over)\s+(?:the\s+)?(?:(?:last|past|previous|prior|multiple|several)\s+)?seasons"
    r"|multi[- ]?seasons?"
    r"|(?:every|each)\s+season"
    r"|(?:multiple|several|many|both|all)\s+years)\b",
    re.I,
)
_UNCONSUMED_GAME_WINDOW = re.compile(
    r"\b(?:last|past|most recent|first|previous|prior|preceding|next)\s+"
    r"(?:(?!(?:round|season|games?|appearances?|and|or)\b)[\w-]+\s+){0,4}(?:games?|appearances?)\b",
    re.I,
)
_OPPONENT_FILTER = re.compile(
    r"\b(?:against|versus|vs\.?)\s+"
    r"(?!(?:most|the most|which (?:team|opponent)|in|during|over|from|at|on)\b)\w",
    re.I,
)
_LEAGUE_REFERENCE = re.compile(
    r"\b(?:league(?:'s|’s)?\s+(?:average|baseline)|"
    r"(?:relative to|compared (?:to|with)|versus|vs\.?)\s+(?:the\s+)?league)\b",
    re.I,
)
_WITHIN_GAME_SCOPE = re.compile(
    r"\b(?:quarters?|halves|half|overtime|clutch|q[1-4]|[1-4]q"
    r"|(?:first|second|third|fourth|[1-4](?:st|nd|rd|th))\s+period"
    r"|period\s+[1-4])\b",
    re.I,
)
_UNSUPPORTED_STAT_THRESHOLD = re.compile(
    r"\b\d+\+\s*(?:[\w/%-]+\s+){0,2}(?:points?|pts|rebounds?|reb|assists?|ast"
    r"|steals?|stl|blocks?|blk|turnovers?|tov|minutes?|min|threes?|3pm|3pa"
    r"|fg3m|fg3a|fgm|fga|ftm|fta|oreb|dreb|pf|fouls?|games?|shooting)\b"
    r"|\b\d+\+\s*(?:point|rebound|assist|steal|block|turnover|minute|three|pt)\b"
    r"|\b\d+[- ](?:point|rebound|assist|steal|block|turnover|minute)\s+games?\b"
    r"|\b(?:double[- ]doubles?|triple[- ]doubles?|double[- ]digits?|50/40/90)\b"
    r"|(?:(?<!\w)(?:>=|<=|>|<|=)\s*-?\d+(?:\.\d+)?|\b(?:at least|more than|over"
    r"|under|less than|fewer than|at most|no more than)\s+-?\d+(?:\.\d+)?%?)"
    r"\s*(?:points?|pts|rebounds?|reb|assists?|ast|steals?|stl|blocks?|blk"
    r"|turnovers?|tov|minutes?|min|threes?|3pm|3pa|fg3m|fg3a|fgm|fga|ftm|fta"
    r"|oreb|dreb|pf|fouls?|shooting|fg|fg%|fg3%|3p%|ft%|ts%|efg%)\b"
    r"|\b\d+(?:\.\d+)?%?\+?\s+or\s+(?:more|fewer|less)\s+(?:points?|pts|rebounds?|reb"
    r"|assists?|ast|steals?|stl|blocks?|blk|turnovers?|tov|minutes?|min|threes?"
    r"|3pm|3pa|fg3m|fg3a|fgm|fga|ftm|fta)\b"
    r"|\b\d+(?:\.\d+)?%\+\s*(?:shooting|fg|fg3|3p|ft|ts|efg)\b"
    r"|\b(?:with|in games with|games with|when (?:he|she|they) "
    r"(?:had|recorded|scored?|shot|played)|where (?:he|she|they) "
    r"(?:had|recorded|scored?|shot|played)|scoring|shooting)\s+"
    r"(?!20\d{2}[-–/])(?:\d+\+?|\d+(?:\.\d+)?%|at least|more than|over|under"
    r"|fewer than|less than|>=|<=|>|<)\b",
    re.I,
)
_UNSUPPORTED_OPPONENT_GROUP = re.compile(
    r"\b(?:eastern|western)\s+conference\b"
    r"|\b(?:east|west)\s+(?:teams?|opponents?)\b|\bin\s+the\s+(?:east|west)\b"
    r"|\b(?:atlantic|central|southeast|northwest|pacific|southwest)\s+division\b"
    r"|\b(?:conference|division)\s+(?:opponents?|teams?|rivals?|matchups?|games?)\b"
    r"|\b(?:playoff|non-playoff|lottery|winning|losing|contending|above[- ]?\.500"
    r"|below[- ]?\.500|sub[- ]?\.500)\s+(?:teams?|opponents?|matchups?)\b"
    r"|\b(?:teams?|opponents?)\s+(?:above|below|over|under)\s+\.?500\b"
    r"|\b(?:top|bottom)[- ]?\d+\s+(?:teams?|opponents?|defenses?|offenses?)\b",
    re.I,
)
_OPPONENT_APPEARANCE_THRESHOLD = re.compile(
    r"\b(?:at least|more than|over|under|less than|fewer than|at most|no more than"
    r"|minimum(?:\s+of)?|min(?:\s+of)?)\s+"
    r"(?:\d+(?:\.\d+)?\+?|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:times?|games?|matchups?|appearances?|meetings?|contests?)\b"
    r"|(?:(?<!\w)(?:>=|<=|>|<|=)\s*\d+(?:\.\d+)?)\s*"
    r"(?:times?|games?|matchups?|appearances?|meetings?|contests?)\b"
    r"|\b\d+\+\s*(?:times?|games?|matchups?|appearances?|meetings?|contests?)\b"
    r"|\b\d+\s+or\s+(?:more|fewer|less)\s+"
    r"(?:times?|games?|matchups?|appearances?|meetings?|contests?)\b"
    r"|\b(?:minimum|min|at least)\s+(?:a\s+)?\d+[- ](?:game|matchup|appearance)\b",
    re.I,
)
_UNSUPPORTED_GAME_PREDICATE = re.compile(
    r"\b(?:(?:winning|losing)\s+(?:games|efforts)|(?:won|lost)\s+games"
    r"|(?:after|following)\s+(?:a\s+)?(?:win|loss)"
    r"|when\s+(?:(?:his|her|their|the)\s+team\s+)?(?:won|wins|lost|loses|winning|losing)"
    r"|only\s+(?:wins|losses)|(?:wins|losses)\s+only"
    r"|(?:in|during|for)\s+(?:the\s+)?(?:(?:home|away|road|team|his|her|their)\s+)?(?:wins|losses|victories|defeats)"
    r"|(?:home|away|road)\s+(?:wins|losses|victories|defeats)"
    r"|back[- ]to[- ]backs?|b2bs?|zero[- ]rest|no rest|well[- ]rested|rested games?"
    r"|on\s+(?:\d+|zero|no|one|two|three)\+?\s+days?(?:\s+of)?\s+rest"
    r"|\d+\+?\s+days?(?:\s+of)?\s+rest|days?\s+off|rest\s+(?:advantage|disadvantage)"
    r"|second\s+night\s+of\s+a\s+back[- ]to[- ]back"
    r"|weekends?|weekdays?|weeknights?|weekend\s+games?|weekday\s+games?"
    r"|mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?"
    r"|as\s+a\s+starter|as\s+a\s+reserve|off\s+the\s+bench|bench\s+(?:games?|appearances?|role)"
    r"|reserve\s+(?:games?|appearances?)|sixth\s+man|starting\s+lineup"
    r"|started|starters?|starts\s+only|in\s+starts|non[- ]starts"
    r"|when\s+(?:starting|coming\s+off\s+the\s+bench)"
    r"|(?:in\s+)?(?:the\s+)?games?\s+(?:that|where|when|he|she|they|with|without|played|started|missed|won|lost)"
    r"|when\s+(?:he|she|they|the\s+team)"
    r"|without\b"
    r"|blowouts?|close\s+games?|garbage\s+time|clutch(?:\s+time)?|regulation\s+games?|\bot"
    r"|national\s+tv|televised|primetime|christmas(?:\s+day)?|opening\s+(?:night|day)|mlk\s+day|all[- ]star(?:\s+break)?"
    r"|foul\s+trouble|fouled\s+out|ejected|ejections?|minute\s+restriction|limited\s+minutes|heavy\s+minutes"
    r"|one[- ]possession\s+games?|double[- ]digit\s+(?:wins?|losses?)"
    r"|(?:without|with)\s+(?:his|her|their|a|the)\s+teammates?"
    r"|teammates?\s+(?:out|absent|injured|sidelined|inactive))\b"
    r"|(?<!\bweek\s)\bstarting\b(?!\s+\d{4}-\d{2}-\d{2}\b)",
    re.I,
)
_UNSUPPORTED_SCOPE = re.compile(
    r"\b(?:career|all[ -]time|excluding|except|without|head[ -]to[ -]head"
    r"|(?:last|past|preceding) [\w-]+ weeks?|play[ -]in|preseason|playoff series|NBA Cup"
    r"|(?:in|during|for) (?:playoff )?game [1-7]"
    r"|today|yesterday|tonight|since|before|after|until)\b",
    re.I,
)
_PER_GAME_METRIC_REPLACEMENTS = {
    "ppg": "points",
    "rpg": "rebounds",
    "apg": "assists",
    "spg": "steals",
    "bpg": "blocks",
    "tpg": "turnovers",
    "mpg": "minutes",
}
_PER_GAME_ALIASES = {
    "ppg": "pts",
    "rpg": "reb",
    "apg": "ast",
    "spg": "stl",
    "bpg": "blk",
    "tpg": "tov",
    "mpg": "min",
}
_AGGREGATION_WORDS = re.compile(
    r"\b(?:total|totals|sum|average|averages|averaging|mean|per[ -]game|a game"
    r"|ppg|rpg|apg|spg|bpg|tpg|mpg)\b",
    re.I,
)
_UNSUPPORTED_AGGREGATION_PATTERNS = (
    re.compile(
        r"\b(?:maximum|max|highest|minimum|lowest|median|std|stddev|stdev"
        r"|standard\s+deviation|variance|percentile|percentiles|rank|ranking|mode|iqr)\b",
        re.I,
    ),
    re.compile(
        r"\bmin\s+(?:of\s+)?(?:points?|pts|rebounds?|reb|assists?|ast|steals?|stl"
        r"|blocks?|blk|turnovers?|tov|minutes?|scoring|fg%|fg3%|3p%|ft%|ts%|efg%"
        r"|plus[ -]minus|value|values|score)\b|\b(?:min/max|max/min)\b",
        re.I,
    ),
)
_UNSUPPORTED_METRIC_PHRASE = re.compile(
    r"\busage(?:\s+(?:rate|percentage|percent|pct))?\b|\busg%?"
    r"|\bplayer\s+efficiency\s+rating\b"
    r"|(?-i:\bPER\b)(?!\s*(?:36|100|48|game|min|appearance|opponent|team))"
    r"|\bper\b(?=\s*(?:$|[,&?]|and\b|by\s+opponent\b))"
    r"|\bbox\s+plus(?:[- ]|\s*/\s*)minus\b|\b[od]?bpm\b"
    r"|\bvalue\s+over\s+replacement(?:\s+player)?\b|\bvorp\b"
    r"|\bwin\s+shares?(?:\s+per\s+48)?\b|\b[od]?ws(?:/48)?\b"
    r"|\b(?:net|offensive|defensive)\s+(?:rating|rtg)\b|\b[nod]rtg\b|\b(?:off|def)\s+rtg\b"
    r"|\b(?:pace|player\s+impact\s+estimate|pie|game\s+score|gmsc)\b"
    r"|\b(?:assist[- ]to[- ]turnover|ast/tov|a/to)(?:\s+ratio)?\b"
    r"|\b(?:turnovers?|assists?|rebounds?|offensive\s+rebounds?|defensive\s+rebounds?"
    r"|steals?|blocks?|tov|ast|reb|oreb|dreb|stl|blk)\s+(?:rate|percentage|percent|pct)\b"
    r"|\b(?:free[- ]?throw\s+(?:attempt\s+(?:rate|percentage|percent|pct)|rate)"
    r"|(?:three|3)[- ]?(?:point(?:er)?|pt)\s+attempt\s+(?:rate|percentage|percent|pct))\b"
    r"|(?<!\w)(?:tov|ast|reb|oreb|dreb|orb|drb|stl|blk)%"
    r"|\b(?:ftr|3par)\b"
    r"|\bper[- ]?(?:100|48|36)\b"
    r"|\b(?:fgm|ftm|field\s+goals?\s+made|free\s+throws?\s+made|2pm|2pa|2p%"
    r"|(?:two|2)[- ]?(?:pointers?|pt\s+(?:shooting\s+)?(?:percentage|percent|pct|makes?|made|attempts?)"
    r"|point\s+(?:shooting\s+)?(?:percentage|percent|pct|makes?|made|attempts?)))\b"
    r"|\b(?:raptor|epm|darko|dpm|estimated\s+plus[- ]minus|deflections?"
    r"|screen\s+assists?|contested\s+shots?|potential\s+assists?|secondary\s+assists?"
    r"|hockey\s+assists?|box[- ]?outs?|charges\s+drawn|rim\s+protection"
    r"|shot\s+quality|expected\s+fg%|xfg%)\b",
    re.I,
)


def _compile_metric_alias(alias: str) -> re.Pattern[str]:
    cleaned = alias.strip()
    if cleaned == "+/-":
        return re.compile(r"(?<!\w)\+/-(?!\w)")
    if cleaned == "points + assists * 2":
        return re.compile(r"(?<!\w)points\s*\+\s*assists\s*\*\s*2(?!\w)", re.I)
    parts = [re.escape(part) for part in re.split(r"[\s_-]+", cleaned) if part]
    body = r"[\s_-]+".join(parts)
    suffix = r"(?!\w)" if cleaned.endswith("%") else r"(?!\w|%)"
    return re.compile(r"(?<!\w)" + body + suffix, re.I)


@lru_cache(maxsize=1)
def _metric_alias_patterns() -> tuple[tuple[re.Pattern[str], str], ...]:
    fantasy_key = load_contract().default_fantasy_metric
    aliases: list[tuple[str, str]] = []
    for metric in load_semantic_catalog().list_metrics():
        key = str(metric["key"])
        for raw in (metric["key"], metric["label"], *metric.get("aliases", [])):
            text = str(raw).strip()
            if not text:
                continue
            target_key = (
                fantasy_key
                if text.casefold()
                in {"fantasy", "fantasy points", "fantasy score", "fantasy scoring"}
                else key
            )
            aliases.append((text, target_key))
    extra_aliases: dict[str, tuple[str, ...]] = {
        "pts_per36": (
            "pts/36",
            "points/36",
            "points per 36 minutes",
            "points per 36",
            "pts per 36 minutes",
            "pts per 36",
            "pts per36",
        ),
        "ast_per36": (
            "ast/36",
            "assists/36",
            "assists per 36 minutes",
            "assists per 36",
            "ast per 36 minutes",
            "ast per 36",
            "ast per36",
        ),
        "fg3_pct": (
            "three point shooting percentage",
            "three point shooting percent",
            "three point shooting pct",
            "three point shooting%",
            "3 point shooting percentage",
            "3 point shooting percent",
            "3 point shooting pct",
            "3 point shooting%",
            "3pt shooting percentage",
            "3pt shooting percent",
            "3pt shooting pct",
            "3pt shooting%",
            "3p shooting percentage",
            "3p shooting percent",
            "3p shooting pct",
            "3p shooting%",
            "three pt shooting percentage",
            "three pt shooting percent",
            "three pt shooting pct",
            "three point percent",
            "three point%",
            "3 point percentage",
            "3 point percent",
            "3 point%",
            "3pt percentage",
            "3pt percent",
            "3pt%",
            "3p%",
            "fg3%",
        ),
        "fg_pct": (
            "field goal shooting percentage",
            "field goal shooting percent",
            "field goal shooting pct",
            "field goal shooting%",
            "fg shooting percentage",
            "fg shooting percent",
            "fg shooting pct",
            "fg shooting%",
            "field goal percent",
            "field goal%",
            "shooting percent",
            "shooting pct",
            "shooting%",
            "fg%",
        ),
        "ft_pct": (
            "free throw shooting percentage",
            "free throw shooting percent",
            "free throw shooting pct",
            "free throw shooting%",
            "ft shooting percentage",
            "ft shooting percent",
            "ft shooting pct",
            "ft shooting%",
            "free throw percent",
            "free throw%",
            "ft%",
        ),
        "ts_pct": (
            "true shooting percent",
            "true shooting pct",
            "true shooting%",
            "ts shooting percentage",
            "ts%",
        ),
        "efg_pct": (
            "effective field goal shooting percentage",
            "effective field goal shooting percent",
            "effective field goal shooting pct",
            "effective fg shooting percentage",
            "effective fg shooting percent",
            "effective fg shooting pct",
            "effective field goal percent",
            "effective field goal%",
            "effective fg percent",
            "effective fg%",
            "efg%",
        ),
        "fg3a": ("three point attempts", "3-point attempts", "3pt attempts", "3pa"),
        "fg3m": (
            "three point makes",
            "3-point makes",
            "three point made",
            "3-point made",
            "3-pointers",
            "three-pointers",
            "threes",
            "3pm",
        ),
        "plus_minus": ("+/-", "plus-minus", "plus/minus", "plus minus"),
        "reb": ("rebounding", "rpg"),
        "ast": ("apg",),
        "stl": ("spg",),
        "blk": ("bpg",),
        "tov": ("tpg",),
        "pts": ("ppg",),
        "min": ("mpg",),
    }
    for key, items in extra_aliases.items():
        for item in items:
            aliases.append((item, key))
    deduped = dict.fromkeys(aliases)
    return tuple((_compile_metric_alias(raw), key) for raw, key in deduped)


def _normalize_venue_phrases(text: str) -> str:
    text = _AWAY_FROM_HOME.sub("away games", text)
    return _VENUE_ONLY_OR_NOUN.sub(
        lambda m: "home games" if (m["home"] or m["only_home"]) else "away games",
        text,
    )


def _extract_metric_spans(
    text: str,
) -> tuple[str, set[str], list[str], list[str]]:
    """Consume supported and unsupported metric phrases longest-non-overlapping first."""
    candidates: list[tuple[int, int, str, str, str]] = []
    for pattern, key in _metric_alias_patterns():
        for match in pattern.finditer(text):
            candidates.append(
                (match.start(), match.end(), "supported", key, match.group(0))
            )
    for match in _UNSUPPORTED_METRIC_PHRASE.finditer(text):
        candidates.append(
            (match.start(), match.end(), "unsupported", "", match.group(0))
        )
    selected: list[tuple[int, int, str, str, str]] = []
    for start, end, kind, key, matched in sorted(
        candidates,
        key=lambda item: (
            -(item[1] - item[0]),
            item[0],
            0 if item[2] == "unsupported" else 1,
        ),
    ):
        if not any(a < end and start < b for a, b, _, _, _ in selected):
            selected.append((start, end, kind, key, matched))
    selected.sort(key=lambda item: item[0])
    remaining = text
    for start, end, _, _, _ in reversed(selected):
        remaining = remaining[:start] + " " + remaining[end:]
    return (
        remaining,
        {key for _, _, kind, key, _ in selected if kind == "supported"},
        [matched for _, _, kind, _, matched in selected if kind == "supported"],
        [matched for _, _, kind, _, matched in selected if kind == "unsupported"],
    )


def _extract_catalog_metrics(text: str) -> tuple[str, set[str], list[str]]:
    """Consume catalog metric aliases longest-non-overlapping first."""
    remaining, keys, matched, _ = _extract_metric_spans(text)
    return remaining, keys, matched


def _resolve_opponent_aggregation(text: str) -> str | None:
    """Resolve aggregation cues including per-game shorthand; reject unsupported ops."""
    for pattern in _UNSUPPORTED_AGGREGATION_PATTERNS:
        if pattern.search(text):
            raise SemanticError(
                "unsupported_scope",
                "Opponent splits support per-game averages, totals, and pooled shooting ratios; maximum, minimum, median, and standard deviation are not supported.",
            )
    cleaned = re.sub(
        r"\btotal\s+(?=rebounds?\s+(?:per[ -]game|a game)\b)", "", text, flags=re.I
    )
    total = bool(re.search(r"\b(?:total|totals|sum)\b", cleaned, re.I))
    average = bool(
        re.search(
            r"\b(?:average|averages|averaging|mean|ppg|rpg|apg|spg|bpg|tpg|mpg"
            r"|per[ -]game|a game)\b",
            cleaned,
            re.I,
        )
    )
    if total and average:
        raise SemanticError(
            "clarification_required", "Choose totals or per-game averages"
        )
    return "total" if total else "average" if average else None


def wants_opponent_splits(question: str) -> bool:
    return bool(_OPPONENT_CUE.search(question)) and not comparison_sides(question)


def opponent_splits_followup(
    question: str,
    previous: str | None,
    *,
    scope: Mapping[str, Any] | None = None,
) -> str | None:
    """Carry the previous split scope forward only for a metric-only reply."""
    if not previous or not wants_opponent_splits(previous):
        return None
    metric_text = re.sub(
        r"^(?:and|what about|how about|show(?: me)?|compare|now)\s+",
        "",
        question.strip(),
        flags=re.I,
    ).strip(" ?.!")
    if not metric_text:
        return None
    rest, keys, _, unsupported_metrics = _extract_metric_spans(metric_text)
    rest_clean = _AGGREGATION_WORDS.sub(" ", rest)
    unsupported_aggs = [
        match.group(0)
        for pattern in _UNSUPPORTED_AGGREGATION_PATTERNS
        for match in pattern.finditer(rest_clean)
    ]
    for pattern in _UNSUPPORTED_AGGREGATION_PATTERNS:
        rest_clean = pattern.sub(" ", rest_clean)
    leftover = re.sub(r"\b(?:and|of)\b|[\s,&/]", "", rest_clean, flags=re.I)
    if not leftover and (keys or unsupported_metrics or unsupported_aggs):
        prior = previous
        if (
            scope
            and scope.get("as_of")
            and not re.search(r"\bas[ -]of\b", prior, re.I)
            and (
                _LAST_GAMES.search(prior)
                or re.search(
                    r"\b(?:last|past|preceding|most recent)\s+\d+\s+"
                    r"(?:(?:calendar\s+)?days?|months?)\b",
                    prior,
                    re.I,
                )
            )
        ):
            prior = f"{prior} as of {scope['as_of']}"
        if (
            scope
            and scope.get("start_date")
            and scope.get("as_of")
            and re.search(
                r"\b(?:this|last|previous|past)\s+(?:month|week)\b", prior, re.I
            )
        ):
            prior = re.sub(
                r"\b(?:this|last|previous|past)\s+(?:month|week)\b",
                f"from {scope['start_date']} through {scope['as_of']}",
                prior,
                flags=re.I,
            )
        return prior + _METRIC_REPLY + metric_text
    return None


def _reject_unsupported_predicates(
    question: str, scope_text: str, round_key: str | None
) -> None:
    round_stripped = _ROUND_PATTERN.sub(" ", scope_text) if round_key else scope_text
    if _UNSUPPORTED_OPPONENT_GROUP.search(
        round_stripped
    ) or _OPPONENT_APPEARANCE_THRESHOLD.search(round_stripped):
        raise SemanticError(
            "unsupported_scope",
            "Opponent splits cover all opponents in the sample and cannot filter opponents by conference, division, tier, or minimum appearances.",
        )
    if _UNSUPPORTED_STAT_THRESHOLD.search(
        scope_text
    ) or _UNSUPPORTED_GAME_PREDICATE.search(scope_text):
        raise SemanticError(
            "unsupported_scope",
            "Opponent splits support a single home or away filter and do not support stat thresholds, game outcomes, rest, or lineup filters.",
        )
    filters = extract_game_filters(scope_text).query_fields
    if set(filters) - {"home_away", "team_abbr"}:
        raise SemanticError(
            "unsupported_scope",
            "Opponent splits support a single home or away filter.",
        )


def _collect_explicit_seasons(question: str, default_season: str) -> list[str]:
    """Collect and validate every season or playoff-year expression before picking one."""
    collected: list[str] = []

    def add_season(candidate: str) -> None:
        try:
            validated = validate_season(candidate)
        except ValueError as exc:
            raise SemanticError(
                "unsupported_coverage", f"Season {candidate} is unavailable"
            ) from exc
        collected.append(validated)

    for match in re.finditer(
        r"\b((?:19|20)\d{2})[-–/]((?:19|20)\d{2}|\d{2})\b(?![-/–]\d)", question
    ):
        start, end = match.groups()
        add_season(f"{start}-{end[-2:]}")
    for match in re.finditer(
        r"\b((?:19|20)\d{2})\s+(?:playoffs?|postseason)\b"
        r"|\b(?:playoffs?|postseason)(?:\s+(?:of|in))?\s+((?:19|20)\d{2})\b(?![-/–]\d)",
        question,
        re.I,
    ):
        year = int(match[1] or match[2])
        add_season(f"{year - 1}-{year % 100:02}")
    if legacy_year := re.search(r"\b(19\d{2})\b(?![-/–]\d)", question):
        raise SemanticError(
            "unsupported_coverage", f"Season {legacy_year[1]} is unavailable"
        )
    if re.search(r"\b(?:last|previous|prior)\s+season\b", question, re.I):
        year = int(default_season[:4]) - 1
        add_season(f"{year}-{(year + 1) % 100:02}")
    if re.search(r"\b(?:this|current)\s+season\b", question, re.I):
        add_season(default_season)
    return collected


def opponent_split_query(question: str, season: str) -> Query:
    """Resolve scope before starting a governed evidence read."""
    question = question.partition(_METRIC_REPLY)[0].split("\nSelected player:", 1)[0]
    question = _SLASH_DATE.sub(r"\1-\2-\3", question)
    if _LEAGUE_REFERENCE.search(question):
        raise SemanticError(
            "unsupported_scope",
            "Opponent splits do not support a league-average comparison. Ask for opponent splits or a league comparison in separate questions.",
        )
    scope_text = re.sub(COLLECTIVE_MATCHUP, " ", question, flags=re.I)
    if _OPPONENT_FILTER.search(scope_text):
        raise SemanticError(
            "unsupported_scope",
            "Opponent splits cover all opponents in the sample. Ask for a separate single-opponent summary.",
        )
    if _UNCONSUMED_GAME_WINDOW.search(_LAST_GAMES.sub(" ", question)):
        raise SemanticError(
            "unsupported_scope",
            "Use a numeric last-N-games window, such as last 3 games.",
        )
    if _WITHIN_GAME_SCOPE.search(question):
        raise SemanticError(
            "unsupported_scope", "Opponent splits are available for whole games only."
        )
    round_key = requested_playoff_round(question)
    if message := round_scope_message(question, round_coverage=True):
        raise SemanticError("unsupported_scope", message)
    phase = "Playoffs" if round_key else explicit_phase(question)
    if phase == "Both":
        raise SemanticError(
            "clarification_required",
            "Choose either the regular season or the playoffs.",
        )
    if _MULTI_SEASON.search(question):
        raise SemanticError(
            "unsupported_scope",
            "Opponent splits cover one NBA season at a time. Choose one season, an explicit date range, or a last-N-games window.",
        )
    intervals = literal_date_intervals(question)
    interval_stripped = re.sub(
        r"(?:\bfrom\s+)?" + ISO_INTERVAL_PATTERN, " ", question, flags=re.I
    )
    if _UNRESOLVED_RANGE.search(interval_stripped):
        raise SemanticError(
            "clarification_required",
            "Use explicit YYYY-MM-DD dates with 'from YYYY-MM-DD through YYYY-MM-DD' or 'between YYYY-MM-DD and YYYY-MM-DD'.",
        )
    if _UNSUPPORTED_SCOPE.search(question):
        raise SemanticError(
            "unsupported_scope",
            "Choose one season or playoff round, an explicit date range, or a last-N-games window.",
        )
    normalized_scope_text = _normalize_venue_phrases(scope_text)
    _reject_unsupported_predicates(question, normalized_scope_text, round_key)
    home, away = venue_mentions(normalized_scope_text)
    if home and away:
        raise SemanticError(
            "unsupported_scope",
            "Opponent splits support a single home or away filter.",
        )
    if _UNRESOLVED_VENUE.search(_RESOLVED_VENUE.sub(" ", normalized_scope_text)):
        raise SemanticError(
            "clarification_required",
            "Clarify the venue filter using 'at home' or 'on the road'.",
        )
    scope = explicit_scope(normalized_scope_text)
    if len(intervals) > 1:
        raise SemanticError(
            "clarification_required", "Use one explicit date range or an as-of date."
        )
    as_of_mentions = re.findall(r"\bas[ -]of\b", question, re.I)
    as_ofs = re.findall(r"\bas[ -]of\s+(\d{4}-\d{2}-\d{2})\b", question, re.I)
    if len(as_of_mentions) != len(as_ofs) or len(as_ofs) > 1:
        raise SemanticError(
            "clarification_required", "Choose one end date for these splits."
        )
    if as_ofs:
        try:
            as_of_date = date.fromisoformat(as_ofs[0])
        except ValueError as exc:
            raise SemanticError(
                "clarification_required", "Please provide a valid as-of date."
            ) from exc
        if as_of_date > semantic_today():
            raise SemanticError(
                "clarification_required",
                "Please choose an as-of date no later than today.",
            )
    week_dates = re.findall(
        r"\bweek\s+(?:of|starting|beginning)\s+(\d{4}-\d{2}-\d{2})\b",
        question,
        re.I,
    )
    if (intervals and week_dates) or len(week_dates) > 1:
        raise SemanticError(
            "clarification_required", "Use one explicit date range or an as-of date."
        )
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", question)
    expected_dates = [
        *(intervals[0] if intervals else ()),
        *as_ofs,
        *week_dates,
    ]
    if len(dates) != len(expected_dates) or set(dates) != set(expected_dates):
        raise SemanticError(
            "clarification_required", "Use one explicit date range or an as-of date."
        )
    if intervals and as_ofs and intervals[0][1] != as_ofs[0]:
        raise SemanticError(
            "clarification_required", "Choose one end date for these splits."
        )
    months = re.findall(MONTH_PATTERN, scope_text, re.I)
    relative_calendar = re.findall(
        r"\b(?:this|last|previous|past)\s+(?:month|week)\b", scope_text, re.I
    )
    day_windows = re.findall(
        r"\b(?:last|past|preceding|most recent)\s+(\d+)\s+(?:calendar\s+)?days?\b",
        scope_text,
        re.I,
    )
    month_windows = re.findall(
        r"\b(?:last|past|preceding|most recent)\s+(\d+)\s+months?\b",
        scope_text,
        re.I,
    )
    last_game_matches = list(_LAST_GAMES.finditer(question))
    window_count = (
        len(intervals)
        + len(week_dates)
        + len(months)
        + len(relative_calendar)
        + len(day_windows)
        + len(month_windows)
        + len(last_game_matches)
    )
    if window_count > 1:
        raise SemanticError(
            "clarification_required",
            "Choose one explicit date range or window for these splits.",
        )
    if (
        len(months) > 1
        or (months and scope.get("window") != "date_range")
        or (months and intervals)
    ):
        raise SemanticError(
            "clarification_required",
            "Choose one explicit date range or single calendar month for these splits.",
        )
    if len(day_windows) > 1 or (day_windows and scope.get("window") != "last_n_days"):
        raise SemanticError(
            "clarification_required", "Choose one window for these splits."
        )
    if len(month_windows) > 1 or (
        month_windows and scope.get("window") != "last_n_months"
    ):
        raise SemanticError(
            "clarification_required", "Choose one window for these splits."
        )
    remaining_time = scope_text
    for pattern in (
        r"\b(?:last|past|preceding|most recent)\s+\d+\s+(?:calendar\s+)?days?\b",
        r"\b(?:last|past|preceding|most recent)\s+\d+\s+months?\b",
        r"\b(?:this|last|previous|past)\s+(?:month|week)\b",
        r"\bweek\s+(?:of|starting|beginning)\s+\d{4}-\d{2}-\d{2}\b",
        r"\b(?:this|last|previous|prior|current)\s+season\b",
        _LAST_GAMES.pattern,
    ):
        remaining_time = re.sub(pattern, " ", remaining_time, flags=re.I)
    if re.search(
        r"\b(?:this|last|past|previous|prior|preceding|most recent|next|current)\s+"
        r"(?:[\w-]+\s+){0,2}(?:days?|weeks?|months?|years?|seasons?)\b"
        r"|\bweek\s+(?:of|starting|beginning)\b",
        remaining_time,
        re.I,
    ):
        raise SemanticError(
            "clarification_required",
            "Choose one explicit date range, single calendar month, or numeric window for these splits.",
        )
    explicit_seasons = _collect_explicit_seasons(question, season)
    if len(set(explicit_seasons)) > 1:
        raise SemanticError(
            "clarification_required", "Choose one NBA season for these splits."
        )
    seasons = [explicit_seasons[0]] if explicit_seasons else [validate_season(season)]
    if scope.get("window") == "date_range":
        dated_seasons = seasons_in_dates(scope["start_date"], scope["as_of"])
        if len(dated_seasons) != 1 or (explicit_seasons and seasons != dated_seasons):
            raise SemanticError(
                "clarification_required", "Choose dates within one matching NBA season."
            )
        seasons = dated_seasons
    if len(seasons) != 1:
        raise SemanticError(
            "clarification_required", "Choose one NBA season for these splits."
        )
    if last_game_matches:
        parsed_windows = {
            (int(match[1]), (match[2] or "").lower()) for match in last_game_matches
        }
        if len(parsed_windows) != 1 or scope.get("window"):
            raise SemanticError(
                "clarification_required", "Choose one window for these splits."
            )
        n_games, qualifier = next(iter(parsed_windows))
        if qualifier == "home":
            venue = "HOME"
        elif qualifier in {"away", "road"}:
            venue = "AWAY"
        else:
            venue = None
        if venue and scope.get("home_away") and scope["home_away"] != venue:
            raise SemanticError(
                "unsupported_scope",
                "Opponent splits support a single home or away filter.",
            )
        if venue:
            scope["home_away"] = venue
        scope.update(window="last_n_games", n=n_games)
    if as_ofs:
        if scope.get("as_of", as_ofs[0]) != as_ofs[0]:
            raise SemanticError(
                "clarification_required", "Choose one end date for these splits."
            )
        scope["as_of"] = as_ofs[0]
    query = Query(
        metric="pts",
        aggregation="average",
        season=seasons[0],
        season_type=phase or load_contract().default_phase,
        playoff_round=round_key,
        **scope,
    )
    query.validate(load_contract().metric(query.metric))
    return query


@dataclass(frozen=True)
class OpponentSplitsRequest:
    query: Query
    player: dict[str, Any]
    metrics: tuple[str, ...]
    aggregation: str = "average"
    canonical_question: str = ""


def parse_opponent_splits(
    question: str,
    players: list[dict[str, Any]],
    season: str,
    *,
    query: Query | None = None,
    player_id: int | None = None,
) -> OpponentSplitsRequest:
    if re.search(r"\bplayer\d+\b", question, re.I):
        raise SemanticError("invalid_scope", "Use a player name.")
    if player_id is not None:
        chosen = next((p for p in players if p["player_id"] == player_id), None)
        if chosen is None:
            raise SemanticError(
                "unsupported_coverage",
                "Selected player is absent from this source scope",
            )
        players = [
            p
            for p in players
            if p["player_id"] == player_id
            or p["player_name"].casefold() != chosen["player_name"].casefold()
        ]
    base, _, metric_reply = question.partition(_METRIC_REPLY)
    base = base.split("\nSelected player:", 1)[0]
    metric_reply = metric_reply.split("\nSelected player:", 1)[0]
    canonical_base, scoped_players = prepare_names(base, players)
    masked, named = mask_player_tokens(canonical_base, scoped_players)
    if len(named) != 1:
        raise SemanticError(
            "clarification_required", "Please name one player for the opponent splits."
        )
    player = next(iter(named.values()))
    if player_id is not None and player["player_id"] != player_id:
        raise SemanticError(
            "unsupported_coverage",
            "Selected player does not match the opponent splits question.",
        )
    query = query or opponent_split_query(canonical_base, season)
    filter_text = _normalize_venue_phrases(
        re.sub(COLLECTIVE_MATCHUP, " ", canonical_base, flags=re.I)
    )
    filters = extract_game_filters(filter_text, [player]).query_fields
    # Uppercase stat shorthand can resemble a team code. MIN is a team code.
    team = filters.get("team_abbr", "")
    if team != "MIN" and explicit_metric_mentions(team):
        filters.pop("team_abbr")
    if set(filters) - {"home_away"}:
        raise SemanticError(
            "unsupported_scope",
            "Opponent splits support a single home or away filter.",
        )
    if (
        filters.get("home_away")
        and query.home_away
        and filters["home_away"] != query.home_away
    ):
        raise SemanticError(
            "unsupported_scope",
            "Opponent splits support a single home or away filter.",
        )
    metric_source = metric_reply or masked
    reply_agg = _resolve_opponent_aggregation(metric_reply) if metric_reply else None
    base_agg = _resolve_opponent_aggregation(masked)
    aggregation = reply_agg or base_agg or "average"
    _, keys, _, unsupported_metrics = _extract_metric_spans(metric_source)
    if unsupported_metrics:
        raise SemanticError(
            "unsupported_scope",
            "Opponent splits support governed box-score, shooting, and per-36 metrics only.",
        )
    contract = load_contract()
    metrics = (
        tuple(key for key in contract.metrics if key in keys)
        if keys
        else DEFAULT_METRICS
    )
    return OpponentSplitsRequest(
        replace(
            query,
            player_id=player["player_id"],
            home_away=filters.get("home_away", query.home_away),
        ),
        {key: player[key] for key in ("player_id", "player_name")},
        metrics,
        aggregation,
        canonical_base,
    )


def _summary(rows: Sequence[Mapping[str, Any]], metric: Metric) -> dict[str, Any]:
    aggregation = (
        "ratio" if metric.denominator else "total" if metric.key == "gp" else "average"
    )
    summary = aggregate(rows, metric, aggregation)
    if not metric.denominator:
        summary["total"] = summary["numerator"]
    return summary


def _record(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    results = [_game_result(row) for row in rows]
    wins, losses = results.count("W"), results.count("L")
    return {
        "games": len(rows),
        "wins": wins,
        "losses": losses,
        "unknown_outcomes": len(rows) - wins - losses,
    }


def compute_opponent_splits(
    evidence: Evidence, request: OpponentSplitsRequest
) -> dict[str, Any]:
    query = request.query
    selection = select_query_rows(evidence, query)
    rows = [row for row in selection.rows if row["player_id"] == query.player_id]
    if not rows:
        raise SemanticError(
            "unsupported_coverage", "No verified player appearances in this scope."
        )
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        opponent = row.get("opponent_abbr")
        if not isinstance(opponent, str) or not re.fullmatch(r"[A-Z]{2,4}", opponent):
            raise SemanticError(
                "incomplete_evidence",
                "Opponent identity is missing from the selected games.",
            )
        groups[opponent].append(row)

    contract = load_contract()
    definitions = {
        key: contract.metric(key)
        for key in dict.fromkeys((*request.metrics, *SCORE_METRICS))
    }
    baseline = {key: _summary(rows, metric) for key, metric in definitions.items()}
    game_values = {
        key: {row["game_id"]: _summary([row], metric)["value"] for row in rows}
        for key, metric in definitions.items()
    }
    scales = {}
    for key in SCORE_METRICS:
        values = [value for value in game_values[key].values() if value is not None]
        if (
            len(values) >= 2
            and (spread := pstdev(values)) > 0
            and baseline[key]["value"] is not None
        ):
            scales[key] = spread
    opponents: list[dict[str, Any]] = []
    for opponent, games in groups.items():
        metrics = {}
        for key, metric in definitions.items():
            summary = _summary(games, metric)
            value, reference = summary["value"], baseline[key]["value"]
            summary["difference"] = (
                (value - reference) * (100 if metric.unit == "ratio" else 1)
                if value is not None and reference is not None
                else None
            )
            metrics[key] = summary
        components = {
            key: (baseline[key]["value"] - metrics[key]["value"]) / spread
            for key, spread in scales.items()
            if metrics[key]["value"] is not None
        }
        opponents.append(
            {
                "opponent_abbr": opponent,
                **_record(games),
                "metrics": metrics,
                "small_sample": len(games) < SMALL_SAMPLE,
                "sample_label": "Small sample" if len(games) < SMALL_SAMPLE else "—",
                "score_components": components,
                "struggle_score": mean(components.values())
                if scales and components.keys() == scales.keys()
                else None,
            }
        )
    ranked = [row for row in opponents if row["struggle_score"] is not None]
    opponents.sort(
        key=lambda row: (
            row["struggle_score"] is None,
            -(row["struggle_score"] or 0),
            -row["games"],
            row["opponent_abbr"],
        )
    )
    # Report extremes only where at least two opponents have distinct scores.
    best = (
        min(
            ranked,
            key=lambda row: (
                row["struggle_score"],
                -row["games"],
                row["opponent_abbr"],
            ),
        )
        if ranked
        else None
    )
    toughest = (
        opponents[0]
        if best
        and not isclose(
            opponents[0]["struggle_score"], best["struggle_score"], abs_tol=1e-9
        )
        else None
    )
    if toughest is None:
        best = None
    games = (
        [
            {
                "game_id": row["game_id"],
                "game_date": str(row["game_date"]),
                "team_abbr": row.get("team_abbr"),
                "opponent_abbr": row["opponent_abbr"],
                "game_result": _game_result(row)
                if _game_result(row) in ("W", "L")
                else None,
                "metrics": {
                    key: {"value": game_values[key][row["game_id"]]}
                    for key in request.metrics
                },
            }
            for row in groups[toughest["opponent_abbr"]]
            if toughest is not None
        ]
        if toughest
        else []
    )
    assumptions = [
        "Struggle score averages the deficits in points, true shooting and plus-minus relative to the player's in-scope baseline. Each deficit is divided by that metric's population standard deviation across the sampled games. Higher means a tougher observed matchup.",
        "Scores use the same available, varying metrics across opponents. Each opponent needs usable data for every active metric. Plus-minus reflects team scoring margin during the player's minutes.",
        "Shooting rates use summed components. Counting stats and shooting rates use games with their required data. Changes are from the player's in-scope per-game averages or pooled rates. Shooting changes are in percentage points.",
    ]
    if any(row["small_sample"] for row in opponents):
        assumptions.append(
            "Opponents with fewer than three appearances are marked as small samples. Their scores can be sensitive to a single game."
        )
    if any(row["unknown_outcomes"] for row in opponents):
        assumptions.append(
            "The W–L record counts verified results. Unknown outcomes are shown separately."
        )
    return {
        "assumptions": assumptions,
        "version": VERSION,
        "player": request.player,
        "scope": {
            "season": query.season,
            "season_type": query.season_type,
            "playoff_round": query.playoff_round,
            "home_away": query.home_away,
            "window": query.window,
            "n": query.n,
            "start_date": str(selection.start) if selection.start else None,
            "as_of": str(selection.end),
            "first_game_date": str(rows[-1]["game_date"]),
            "last_game_date": str(rows[0]["game_date"]),
            "source": evidence.source,
            "snapshot_id": evidence.snapshot_id,
            "data_through": evidence.data_through[selection.scopes[0]],
        },
        "metric_definitions": {
            key: {"key": key, "unit": metric.unit, "label": metric.label}
            for key, metric in definitions.items()
        },
        "display_metrics": list(request.metrics),
        "aggregation": request.aggregation,
        "baseline": {**_record(rows), "metrics": baseline},
        "score_scales": scales,
        "opponents": opponents,
        "toughest_opponent": toughest["opponent_abbr"] if toughest else None,
        "best_opponent": best["opponent_abbr"] if best else None,
        "toughest_opponent_games": games,
    }


def answer_opponent_splits(
    question: str,
    evidence: Evidence,
    players: list[dict[str, Any]],
    season: str,
    *,
    query: Query | None = None,
    player_id: int | None = None,
) -> dict[str, Any]:
    from app.agent.claim_renderers import opponent_splits_tables

    request = parse_opponent_splits(
        question, players, season, query=query, player_id=player_id
    )
    result = compute_opponent_splits(evidence, request)
    prior_question = (
        request.canonical_question
        or question.partition(_METRIC_REPLY)[0].split("\nSelected player:", 1)[0]
    )
    if request.query.season not in prior_question:
        prior_question += f" ({request.query.season})"
    if not request.query.playoff_round and not explicit_phase(prior_question):
        prior_question += f" {request.query.season_type}"
    if (
        request.query.window in {"last_n_games", "last_n_days", "last_n_months"}
        and not request.query.as_of
    ):
        prior_question += f" as of {result['scope']['as_of']}"
    if (
        request.query.window == "date_range"
        and request.query.start_date
        and request.query.as_of
        and re.search(
            r"\b(?:this|last|previous|past)\s+(?:month|week)\b", prior_question, re.I
        )
    ):
        prior_question = re.sub(
            r"\b(?:this|last|previous|past)\s+(?:month|week)\b",
            f"from {request.query.start_date} through {request.query.as_of}",
            prior_question,
            flags=re.I,
        )
    if _resolve_opponent_aggregation(prior_question) != request.aggregation:
        for alias, replacement in _PER_GAME_METRIC_REPLACEMENTS.items():
            prior_question = re.sub(
                rf"\b{alias}\b", replacement, prior_question, flags=re.I
            )
        prior_question = " ".join(_AGGREGATION_WORDS.sub(" ", prior_question).split())
        if request.aggregation == "total":
            prior_question += " total"
        elif _resolve_opponent_aggregation(prior_question) is not None:
            prior_question += " per game"
    scope_context: dict[str, Any] = {
        "season": request.query.season,
        "window": request.query.window,
        "as_of": result["scope"]["as_of"],
    }
    if request.query.n is not None:
        scope_context["n"] = request.query.n
    if request.query.start_date is not None:
        scope_context["start_date"] = request.query.start_date
    payload = answer_payload(
        "",
        status="ok",
        ask_route={"route": "opponent_splits", "source": "deterministic"},
        opponent_splits=result,
        semantic_plan={"status": "opponent_splits", "model_calls": 0},
        conversation_context={
            "analysis_type": "opponent_splits",
            "players": [request.player],
            "scope": scope_context,
            "opponent_splits_question": prior_question,
        },
    )
    opponent_splits_tables(payload)
    return payload


def _matches_saved_player_exactly(opp_question: str, player: dict[str, Any]) -> bool:
    spans = longest_player_spans(opp_question, [player], normalize_spaces=True)
    if not spans:
        return False
    for span in spans:
        trailing = opp_question[span.end :].lstrip()
        if re.match(r"^(?:jr|sr|ii|iii|iv|v)\b", trailing, re.I):
            continue
        next_word = re.match(r"^([^\W\d_]+)", trailing)
        if (
            next_word
            and next_word[1][0].isupper()
            and normalize_player_text(next_word[1]) not in _SCOPE_WORDS
        ):
            continue
        leading = opp_question[: span.start].rstrip()
        prev_word = re.search(r"([^\W\d_]+)$", leading)
        if (
            prev_word
            and prev_word[1][0].isupper()
            and normalize_player_text(prev_word[1]) not in _SCOPE_WORDS
        ):
            continue
        return True
    return False


def hydrate_opponent_splits_context(
    context: dict[str, Any], default_season: str
) -> dict[str, Any]:
    """Validate recovered opponent-splits hints before storing them in session state."""
    opp_question = context.get("opponent_splits_question")
    if context.get("analysis_type") != "opponent_splits" and not opp_question:
        return context
    if not isinstance(opp_question, str) or not opp_question.strip():
        raise ValueError("Missing saved opponent splits question")
    if (
        not wants_opponent_splits(opp_question)
        or split_kind(opp_question)
        or wants_award(opp_question)
        or comparison_sides(opp_question)
    ):
        raise ValueError("Invalid saved opponent splits route")
    players = context.get("players") or []
    if len(players) != 1:
        raise ValueError("Saved opponent splits require one player")
    player = players[0]
    player_id = player.get("player_id")
    player_name = str(player.get("player_name") or "").strip()
    if type(player_id) is not int or player_id < 1 or not player_name:
        raise ValueError("Invalid saved opponent splits player")
    if not _matches_saved_player_exactly(
        opp_question, {"player_id": player_id, "player_name": player_name}
    ):
        raise ValueError("Saved player does not match saved opponent splits question")
    scope = context.get("scope") or {}
    season = scope.get("season") or default_season
    query = opponent_split_query(opp_question, season)
    if scope.get("season") and query.season != scope["season"]:
        raise ValueError("Saved season does not match saved opponent splits question")
    saved_as_of = scope.get("as_of")
    if saved_as_of is not None:
        as_of_str = str(saved_as_of)
        as_of_date = date.fromisoformat(as_of_str)
        if as_of_date > semantic_today():
            raise ValueError("Saved as-of date is in the future")
        if query.as_of and query.as_of != as_of_str:
            raise ValueError(
                "Saved as-of date does not match saved opponent splits question"
            )
        if (
            query.window in {"last_n_games", "last_n_days", "last_n_months"}
            and query.as_of is None
        ):
            opp_question = f"{opp_question} as of {as_of_str}"
            query = opponent_split_query(opp_question, season)
            context = {**context, "opponent_splits_question": opp_question}
    saved_start = scope.get("start_date")
    if (
        saved_start is not None
        and saved_as_of is not None
        and query.window == "date_range"
        and re.search(
            r"\b(?:this|last|previous|past)\s+(?:month|week)\b", opp_question, re.I
        )
    ):
        opp_question = re.sub(
            r"\b(?:this|last|previous|past)\s+(?:month|week)\b",
            f"from {saved_start} through {saved_as_of}",
            opp_question,
            flags=re.I,
        )
        opponent_split_query(opp_question, season)
        context = {**context, "opponent_splits_question": opp_question}
    return dict(context, analysis_type="opponent_splits")
