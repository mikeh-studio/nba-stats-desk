"""Bounded literal game predicates shared by every governed Ask handler.

Only recognized clauses are consumed. Unknown wording stays in the question so
the normal full-match grammar can withhold it rather than broadening the scope.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.agent.semantics import SemanticError

FILTER_FIELDS = (
    "team_abbr",
    "opponent_abbr",
    "home_away",
    "game_result",
    "weekdays",
    "stat_filters",
)
WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
COMPONENTS = {
    "offensive rebounds": "oreb",
    "defensive rebounds": "dreb",
    "personal fouls": "pf",
    "fouls": "pf",
    "points": "pts",
    "rebounds": "reb",
    "assists": "ast",
    "steals": "stl",
    "blocks": "blk",
    "turnovers": "tov",
    "minutes": "min",
    "field goals made": "fgm",
    "field goal attempts": "fga",
    "three pointers made": "fg3m",
    "three point attempts": "fg3a",
    "free throws made": "ftm",
    "free throw attempts": "fta",
    "plus minus": "plus_minus",
    "plus-minus": "plus_minus",
    **{
        key: key
        for key in (
            "pts",
            "reb",
            "ast",
            "stl",
            "blk",
            "tov",
            "min",
            "fgm",
            "fga",
            "fg3m",
            "fg3a",
            "ftm",
            "fta",
            "oreb",
            "dreb",
            "pf",
            "plus_minus",
        )
    },
}
_COMPONENT = "|".join(re.escape(k) for k in sorted(COMPONENTS, key=len, reverse=True))
_OPS = {
    "at least": "gte",
    "more than": "gt",
    "over": "gt",
    "at most": "lte",
    "no more than": "lte",
    "fewer than": "lt",
    "less than": "lt",
    "under": "lt",
    "exactly": "eq",
    ">=": "gte",
    "<=": "lte",
    ">": "gt",
    "<": "lt",
    "=": "eq",
}
_OP = "|".join(re.escape(k) for k in sorted(_OPS, key=len, reverse=True))


@dataclass(frozen=True)
class GameFilterExtraction:
    cleaned_question: str
    query_fields: dict[str, Any]


def _league_operand_boundary(text: str, identity_spans) -> int | None:
    """Locate the binary boundary before consuming any predicate clause.

    League labels may follow calendar qualifiers or lead a comma-delimited
    complement. Source-name masking keeps names from acting as grammar tokens.
    """
    league = re.search(r"\bleague\s+(?:average|baseline)\b", text, re.I)
    if league is None:
        return None
    prefix = re.match(
        r"\s*(?:compared\s+(?:to|with)|against|versus|vs\.?)\s+", text, re.I
    )
    focal_positions = [start for start, _ in identity_spans if start > league.end()]
    if prefix:
        boundaries = [
            position
            for position in (text.find(",", league.end()), *focal_positions)
            if position >= league.end()
        ]
        continuation = re.search(
            r"\b(?:how|what|did|does)\b", text[league.end() :], re.I
        )
        if continuation:
            boundaries.append(league.end() + continuation.start())
        return min(boundaries) if boundaries else None
    before = text[: league.start()]
    comma = before.rfind(",")
    links = list(re.finditer(r"\b(?:against|versus|vs\.?|with|to)\b", before, re.I))
    boundary = max(comma, links[-1].start() if links else -1)
    if boundary >= 0:
        return boundary
    after_comma = text.find(",", league.end())
    if after_comma >= 0 and not any(
        start < league.start() for start, _ in identity_spans
    ):
        return after_comma
    return league.start()


def _validate_league_filter_ownership(text, bound, identity_spans) -> None:
    boundary = _league_operand_boundary(text, identity_spans)
    if boundary is None:
        return
    league = re.search(r"\bleague\s+(?:average|baseline)\b", text, re.I)
    assert league is not None
    operand_start = min([league.start(), *[start for start, _ in identity_spans]])

    def is_global(start):
        if identity_spans and start < operand_start:
            return True
        prefix = text[:start].strip(" ,\t\n")
        return not prefix or bool(
            re.fullmatch(
                r"(?:in|for|during|over)\s+(?:the\s+)?20\d{2}[-–/]\d{2}(?:\s+(?:regular season|playoffs|season))?",
                prefix,
                re.I,
            )
        )

    before: set[tuple[str, str]] = set()
    after: set[tuple[str, str]] = set()
    for key, value, (start, _) in bound:
        if is_global(start):
            continue
        item = (key, json.dumps(value, sort_keys=True))
        (before if start < boundary else after).add(item)
    if before and before != after:
        raise SemanticError(
            "unsupported_scope",
            "The player and league operands specify different game filters. State one shared filter after both operands, repeat the same filter for both, or ask separate questions.",
        )


def extract_game_filters(question: str, players=()) -> GameFilterExtraction:
    text = question
    identity_spans: list[tuple[int, int]] = []
    for player in players:
        for name in (player["player_name"], *player.get("aliases", [])):
            if name:
                identity_spans.extend(
                    match.span()
                    for match in re.finditer(
                        r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I
                    )
                )
                text = re.sub(
                    r"(?<!\w)" + re.escape(name) + r"(?!\w)",
                    lambda m: " " * len(m[0]),
                    text,
                    flags=re.I,
                )
    fields: dict[str, Any] = {}
    spans: list[tuple[int, int]] = []
    bound = []

    def bind(key, value, match):
        if key in fields and fields[key] != value:
            raise SemanticError(
                "unsupported_scope",
                "State one shared game filter. Contradictory or separate player/league filters require separate questions.",
            )
        fields[key] = value
        spans.append(match.span())
        bound.append((key, value, match.span()))

    # Longer expressions first; every expression includes its scope connector.
    outcomes = (
        (
            "W",
            r"\b(?:in (?:wins|victories)(?: only)?|(?:wins|victories)[ -]only|only (?:in )?(?:wins|victories)|in games (?:that )?(?:(?:(?:his|her|their|the) team|they|he|she) )?won)(?!['’])\b",
        ),
        (
            "L",
            r"\b(?:in (?:losses|defeats)(?: only)?|(?:losses|defeats)[ -]only|only (?:in )?(?:losses|defeats)|in games (?:that )?(?:(?:(?:his|her|their|the) team|they|he|she) )?lost)\b",
        ),
    )
    for value, pattern in outcomes:
        for match in re.finditer(pattern, text, re.I):
            bind("game_result", value, match)
    for value, pattern in (
        ("HOME", r"\b(?:at home|in home games|home games)\b"),
        (
            "AWAY",
            r"\b(?:on the road|in (?:away|road) games|(?:away|road) games|games away)\b",
        ),
    ):
        for match in re.finditer(pattern, text, re.I):
            bind("home_away", value, match)
    days = "|".join(WEEKDAYS)
    for match in re.finditer(
        rf"\bon ((?:{days})s?(?:\s*(?:,|and|or)\s*(?:{days})s?)*)\b", text, re.I
    ):
        selected_days = sorted(
            {
                next(
                    i
                    for i, day in enumerate(WEEKDAYS)
                    if day.casefold() == found.casefold()
                )
                for found in re.findall(days, match[1], re.I)
            }
        )
        bind("weekdays", selected_days, match)
    for match in re.finditer(
        rf"\b(?:with|where (?:he|she|they) (?:had|recorded)|in games with)\s+(?P<op>{_OP})\s+(?P<value>-?\d+(?:\.\d+)?)\s+(?P<component>{_COMPONENT})(?!\w)",
        text,
        re.I,
    ):
        numeric_literal = match["value"]
        numeric_value = float(numeric_literal)
        if Decimal(numeric_literal) != Decimal(str(numeric_value)):
            raise SemanticError(
                "clarification_required",
                "That statistic threshold has more precision than the supported numeric contract can preserve. Restate it with a representable decimal threshold.",
            )
        rule = {
            "component": COMPONENTS[match["component"].casefold()],
            "op": _OPS[match["op"].casefold()],
            "value": numeric_value,
        }
        fields.setdefault("stat_filters", []).append(rule)
        spans.append(match.span())
        bound.append(("stat_filters", rule, match.span()))
    for component in {rule["component"] for rule in fields.get("stat_filters", [])}:
        rules = [
            rule for rule in fields["stat_filters"] if rule["component"] == component
        ]
        lower = (
            max(
                (rule["value"], rule["op"] == "gt")
                for rule in rules
                if rule["op"] in {"gt", "gte", "eq"}
            )
            if any(rule["op"] in {"gt", "gte", "eq"} for rule in rules)
            else None
        )
        upper = (
            min(
                (rule["value"], rule["op"] != "lt")
                for rule in rules
                if rule["op"] in {"lt", "lte", "eq"}
            )
            if any(rule["op"] in {"lt", "lte", "eq"} for rule in rules)
            else None
        )
        if (
            lower is not None
            and upper is not None
            and (
                lower[0] > upper[0]
                or (lower[0] == upper[0] and (lower[1] or not upper[1]))
            )
        ):
            raise SemanticError(
                "unsupported_scope",
                "The per-game statistic thresholds contradict each other. State one consistent range.",
            )
    # Team labels are literal abbreviations; loaded source evidence authorizes
    # them at execution. A comparison's "against league" is never a team token.
    for key, pattern in (
        ("opponent_abbr", r"\b(?:against|versus|vs\.?|opponent)\s+([A-Z]{2,3})(?!\w)"),
        ("team_abbr", r"\b(?:for|on|with)\s+(?:the\s+)?([A-Z]{2,3})(?!\w)"),
    ):
        for match in re.finditer(pattern, text):
            bind(key, match[1], match)
    if spans:
        for start, _ in spans:
            if re.search(
                r"\b(?:not|except|excluding|without|rather than)\s*$",
                text[:start],
                re.I,
            ):
                raise SemanticError(
                    "unsupported_scope",
                    "State the included game filters affirmatively; negated filters require a separate explicit scope.",
                )
        unconsumed = text
        for start, end in sorted(spans, reverse=True):
            unconsumed = unconsumed[:start] + " " * (end - start) + unconsumed[end:]
        if re.search(r"\bor\b", unconsumed[min(start for start, _ in spans) :], re.I):
            raise SemanticError(
                "unsupported_scope",
                "Alternative game-filter conditions require separate questions; supported predicates select their shared intersection.",
            )
        if (
            "game_result" in fields
            and re.search(r"\b(?:wins|victories)\b", text, re.I)
            and re.search(r"\b(?:losses|defeats)\b", text, re.I)
        ):
            raise SemanticError(
                "unsupported_scope",
                "Choose wins or losses for one shared baseline. Separate outcome baselines require separate questions.",
            )
        # A local predicate on one binary operand must not become a shared
        # predicate. A trailing filter after both operands is explicitly shared.
        first = min(start for start, _ in spans)
        _validate_league_filter_ownership(text, bound, identity_spans)
        link = re.search(
            r"\b(?:(?:against|with|to)\s+(?:the\s+)?league|versus|vs\.?)\b",
            text,
            re.I,
        )

        def same_both_sides(boundary):
            before = {
                (key, json.dumps(value, sort_keys=True))
                for key, value, (start, _) in bound
                if start < boundary
            }
            after = {
                (key, json.dumps(value, sort_keys=True))
                for key, value, (start, _) in bound
                if start >= boundary
            }
            return bool(before) and before == after

        if (
            not re.search(r"\bleague\s+(?:average|baseline)\b", text, re.I)
            and link
            and first < link.start()
            and not re.match(r"\s*(?:in |at |on |with |only )", text, re.I)
            and not same_both_sides(link.end())
        ):
            raise SemanticError(
                "unsupported_scope",
                "Place one shared game filter after both comparison operands, or ask each filtered scope separately.",
            )
        compare_and = re.match(r"\s*compare\s+.+?\s+(and|with|to)\s+", text, re.I)
        if (
            compare_and
            and not re.search(r"\bleague\s+(?:average|baseline)\b", text, re.I)
            and (not link or compare_and.end() < link.start())
            and first < compare_and.end()
            and first > compare_and.start()
            and not same_both_sides(compare_and.end())
        ):
            raise SemanticError(
                "unsupported_scope",
                "Place one shared game filter after both named players, or ask each filtered scope separately.",
            )
        if re.search(
            r"\b(?:but|whereas|respectively|only for|for (?:the )?(?:player|league) only)\b",
            text,
            re.I,
        ):
            raise SemanticError(
                "unsupported_scope",
                "Separate player or league filters require separate questions.",
            )
    text = question
    for (_, previous_end), (next_start, _) in zip(sorted(spans), sorted(spans)[1:]):
        if re.fullmatch(r"\s*(?:and|,)\s*", text[previous_end:next_start], re.I):
            spans.append((previous_end, next_start))
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + " " + text[end:]
    cleaned = re.sub(r"[^\S\n]+", " ", text).strip() if spans else question
    return GameFilterExtraction(cleaned, fields)


def filter_scope(scope: dict[str, Any]) -> dict[str, Any]:
    return {key: scope[key] for key in FILTER_FIELDS if scope.get(key) is not None}


def filter_description(scope: dict[str, Any]) -> str:
    parts = []
    if scope.get("team_abbr"):
        parts.append("appearance team " + scope["team_abbr"])
    if scope.get("opponent_abbr"):
        parts.append("opponent " + scope["opponent_abbr"])
    if scope.get("home_away"):
        parts.append(scope["home_away"].lower() + " games")
    if scope.get("game_result"):
        parts.append("wins" if scope["game_result"] == "W" else "losses")
    if scope.get("weekdays"):
        parts.append(" or ".join(WEEKDAYS[i] for i in scope["weekdays"]))
    for rule in scope.get("stat_filters") or []:
        operators = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "eq": "="}
        exact_value = format(Decimal(str(rule["value"])), "f")
        parts.append(
            f"{rule['component']} {operators[rule['op']]} {exact_value} per appearance"
        )
    return "Shared game filters: " + "; ".join(parts) + "." if parts else ""


def filter_question(scope: dict[str, Any]) -> str:
    """Canonical executable clauses for saved follow-ups and browser recovery."""
    clauses = []
    if scope.get("team_abbr"):
        clauses.append("for " + scope["team_abbr"])
    if scope.get("opponent_abbr"):
        clauses.append("against " + scope["opponent_abbr"])
    if scope.get("home_away"):
        clauses.append("at home" if scope["home_away"] == "HOME" else "on the road")
    if scope.get("game_result"):
        clauses.append("in wins" if scope["game_result"] == "W" else "in losses")
    if scope.get("weekdays"):
        clauses.append(
            "on " + " or ".join(WEEKDAYS[i] + "s" for i in scope["weekdays"])
        )
    reverse_ops = {
        "gte": "at least",
        "gt": "more than",
        "lte": "at most",
        "lt": "fewer than",
        "eq": "exactly",
    }
    for rule in scope.get("stat_filters") or []:
        exact_value = format(Decimal(str(rule["value"])), "f")
        clauses.append(
            f"with {reverse_ops[rule['op']]} {exact_value} {rule['component']}"
        )
    return " ".join(clauses)
