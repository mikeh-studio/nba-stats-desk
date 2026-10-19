"""Planner-only aggregation intent; executable queries never contain unspecified."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from app.agent.planner import explicit_metric_mentions
from app.agent.question_intent import explicit_aggregation, task_terms
from app.agent.semantics import SemanticError, load_contract

SEMANTIC_PLANNER_CONTRACT = "semantic_intent/2"


@dataclass(frozen=True)
class AggregationIntent:
    value: str
    source: str

    @classmethod
    def decode(cls, query: Mapping[str, Any]) -> AggregationIntent:
        value, source = query.get("aggregation"), query.get("aggregation_source")
        if not isinstance(source, str) or source not in {
            "explicit",
            "inherited",
            "planner_selected",
            "omitted",
        }:
            raise SemanticError(
                "invalid_plan", "Missing or invalid aggregation provenance"
            )
        if (
            not isinstance(value, str)
            or value not in {"average", "total", "ratio", "unspecified"}
            or ((value == "unspecified") != (source == "omitted"))
        ):
            raise SemanticError("invalid_plan", "Invalid aggregation intent")
        return cls(value, source)


def resolve_aggregation_intent(
    question: str,
    plan: dict[str, Any],
    replacements: Mapping[str, str],
    references: Mapping[str, str],
    selected_season: str,
    context: Mapping[str, Any],
    eligible: bool,
) -> dict[str, Any] | None:
    """Resolve only the approved default, after checking independently known scope.

    Canonical literal cues constrain model interpretation; this is not a complete
    natural-language recognizer. Noneligible planner selections retain their value.
    """
    from app.agent.semantic_planner import explicit_scope
    from app.agent.semantic_serving import has_time_scope

    text = " ".join(question.casefold().split())
    current_names = {
        references[handle]
        for name, handle in replacements.items()
        if re.search(
            r"(?<!\w)" + re.escape(" ".join(name.casefold().split())) + r"(?!\w)", text
        )
    }
    named_points = explicit_metric_mentions(question) == {"pts"}
    other_task = task_terms(question) - {"point", "pts", "total"}
    # Task markers describe requests independently of the model's query fields.
    summary = not other_task and not re.search(
        r"\b(?:percen(?:tile|tage)|compare|versus|vs|game[ -]by[ -]game|trend|chart)\b",
        text,
    )
    cue = explicit_aggregation(question)
    if named_points and summary and not cue:
        from app.agent.overview_calendar import validate_single_period

        validate_single_period(question)
    from app.agent.time_defaults import resolve_time_defaults

    resolved_time, _, _ = resolve_time_defaults(question, context=dict(context))
    current_scope = explicit_scope(resolved_time)
    complete_request = bool(
        len(current_names) == 1
        and has_time_scope(question)
        and explicit_metric_mentions(question)
    )
    complete = bool(
        len(current_names) == 1
        and current_scope.get("window") == "date_range"
        and named_points
        and summary
    )
    previous_scope = context.get("scope") or {}
    previous = previous_scope.get("aggregation") or context.get("aggregation")
    previous_metric = previous_scope.get("metric") or next(
        iter(context.get("metrics", [])), None
    )
    if previous_metric == "gp" or previous_scope.get("operation") == "game_log":
        previous = None
    if eligible and complete:
        from app.agent.overview_calendar import validate_phase_venue

        validate_phase_venue(question)
        if not cue and re.search(r"\bagainst\b", question, re.I):
            raise SemanticError(
                "clarification_required",
                "Specify total points or points per game for an opponent-filtered request",
            )
    policy = None
    for query in plan["queries"]:
        if not isinstance(query, dict):
            raise SemanticError("invalid_plan", "Query intent must be an object")
        intent = AggregationIntent.decode(query)
        metric = load_contract().metrics.get(str(query.get("metric", "")))
        fixed = (
            "ratio"
            if metric and metric.denominator
            else "total"
            if query.get("metric") == "gp" or query.get("operation") == "game_log"
            else None
        )
        threshold_total = (
            fixed == "ratio"
            and cue == "total"
            and bool(
                re.search(
                    r"\btotal\s+(?:fga|fg3a|fta|(?:field[ -]goal |three[ -]point |free[ -]throw )?attempts?|shots?)\b",
                    question,
                    re.I,
                )
            )
        )
        expected_cue = (
            None if threshold_total else fixed if fixed and cue == "average" else cue
        )
        convention_cue = expected_cue is not None and expected_cue != cue
        inherited = (
            previous
            if context
            and not complete_request
            and not fixed
            and previous in {"average", "total"}
            else None
        )
        if expected_cue and (
            intent.value != expected_cue
            or intent.source
            not in (
                {"explicit", "planner_selected"} if convention_cue else {"explicit"}
            )
        ):
            raise SemanticError(
                "invalid_plan", "Planned aggregation contradicts the current request"
            )
        if (
            not cue
            and inherited
            and (intent.value != inherited or intent.source != "inherited")
        ):
            raise SemanticError(
                "invalid_plan", "Planned aggregation contradicts the previous analysis"
            )
        if intent.source == "inherited" and not inherited:
            raise SemanticError(
                "invalid_plan", "No matching aggregation is available to inherit"
            )
        if eligible and complete:
            phase = current_scope.get("season_type") or (
                "Playoffs"
                if re.search(r"\b(?:playoffs?|postseason)\b", text)
                else load_contract().default_phase
            )
            seasons = {
                a + "-" + b[-2:]
                for a, b in re.findall(r"\b(20\d{2})-(\d{4}|\d{2})(?!-\d)\b", text)
            }
            expected = dict(
                current_scope,
                season=next(iter(seasons)) if len(seasons) == 1 else selected_season,
                season_type=phase,
                player_name=next(iter(current_names)),
                metric="pts",
                operation="summary",
            )
            if (
                len(seasons) > 1
                or plan["status"] != "query"
                or any(
                    query.get(
                        key,
                        expected[key]
                        if key
                        in {
                            "window",
                            "start_date",
                            "as_of",
                            "n",
                            "season_type",
                            "operation",
                        }
                        else None,
                    )
                    != value
                    for key, value in expected.items()
                )
                or (
                    not cue
                    and any(
                        query.get(k) is not None
                        for k in (
                            "team_abbr",
                            "opponent_abbr",
                            "min_games",
                            "min_attempts",
                        )
                    )
                )
                or query.get("home_away") != current_scope.get("home_away")
            ):
                raise SemanticError(
                    "invalid_plan",
                    "Planned points summary contradicts the requested identity, dates or scope",
                )
            if not cue:
                if intent.source != "omitted":
                    raise SemanticError(
                        "invalid_plan",
                        "An omitted dated-points aggregation must remain unspecified until policy resolution",
                    )
                policy = {
                    "policy": "dated_points_per_game_v2",
                    "source": "deterministic_product_policy",
                    "reason": "aggregation_omitted",
                    "original_aggregation": "unspecified",
                    "chosen_aggregation": "average",
                    "unit": "points per game",
                    "request_scope": expected,
                    "derived_scope_fields": sorted(
                        k
                        for k in expected
                        if k not in query
                        and k in {"window", "start_date", "as_of", "n", "season_type"}
                    ),
                }
                query["aggregation"] = "average"
        if query["aggregation"] == "unspecified":
            raise SemanticError(
                "clarification_required", "Specify an aggregation for this request"
            )
        query.pop("aggregation_source")
    return policy
