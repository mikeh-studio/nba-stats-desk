"""Planner-only aggregation intent; executable queries never contain unspecified."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from app.agent.scope_policy import (
    explicit_aggregation,
    explicit_metric_mentions,
    task_terms,
)
from app.agent.semantics import SemanticError, load_contract

SEMANTIC_PLANNER_CONTRACT = "semantic_intent/3"


@dataclass(frozen=True)
class AggregationIntent:
    value: str
    source: str | None

    @classmethod
    def decode(cls, query: Mapping[str, Any]) -> AggregationIntent:
        value, source = query.get("aggregation"), query.get("aggregation_source")
        # Advisory metadata is bounded for diagnostics, never execution authority.
        if not isinstance(source, str) or source not in {
            "explicit",
            "inherited",
            "planner_selected",
            "omitted",
        }:
            source = None
        if not isinstance(value, str) or value not in {
            "average",
            "total",
            "ratio",
            "unspecified",
        }:
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
    from app.agent.scope_policy import explicit_scope
    from app.agent.semantic_serving import has_time_scope, requested_seasons

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
    dated_candidate = bool(
        eligible and named_points and summary and len(current_names) == 1
    )
    from app.agent.overview_calendar import MONTH_PATTERN, since_month_start

    open_start = since_month_start(question) if dated_candidate and not cue else None
    if open_start and (
        current_scope.get("window") is not None
        or re.search(
            r"\b(?:past|last|preceding|prior|previous)\s+\d+\s+(?:games?|days?|weeks?|months?)\b",
            question,
            re.I,
        )
    ):
        raise SemanticError(
            "clarification_required", "Provide one window for the since-month summary"
        )
    unresolved_text = re.sub(r"^may i (?:see|have|get)\s+", "", question, flags=re.I)
    for name in replacements:
        unresolved_text = re.sub(
            r"(?<!\w)" + re.escape(name) + r"(?!\w)", "", unresolved_text, flags=re.I
        )
    unresolved_text = re.sub(
        r"\bas[ -]of\s+\d{4}-\d{2}-\d{2}\b", "", unresolved_text, flags=re.I
    )
    if (
        dated_candidate
        and not cue
        and current_scope.get("window") is None
        and open_start is None
        and (
            re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b", unresolved_text)
            or re.search(MONTH_PATTERN, unresolved_text, re.I)
            or re.search(
                r"\b(?:since|before|after|until|through)\s+20\d{2}\b(?![-/]\d)",
                unresolved_text,
                re.I,
            )
            or any(
                isinstance(q, dict) and q.get("window") == "date_range"
                for q in plan["queries"]
            )
        )
    ):
        raise SemanticError(
            "clarification_required",
            "Please provide one unambiguous date interval for this points summary",
        )
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
    if eligible and (complete or open_start):
        from app.agent.overview_calendar import validate_phase_venue

        validate_phase_venue(question)
        from app.agent.overview_calendar import (
            literal_date_intervals,
            validate_single_period,
        )

        if literal_date_intervals(question):
            validate_single_period(question)
        if not cue and re.search(r"\bagainst\b", question, re.I):
            raise SemanticError(
                "clarification_required",
                "Specify total points or points per game for an opponent-filtered request",
            )
    phase = current_scope.get("season_type") or (
        "Playoffs"
        if re.search(r"\b(?:playoffs?|postseason)\b", text)
        else load_contract().default_phase
    )
    seasons = (
        requested_seasons(question, selected_season)
        if eligible and (complete or open_start)
        else []
    )
    partial_phase = None
    if not complete_request:
        partial_phase = (
            phase
            if re.search(
                r"\b(?:regular[ -]season|playoffs?|postseason)\b", question, re.I
            )
            else previous_scope.get("season_type")
        )
    policy = None
    resolutions = []
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
        inherited = (
            previous
            if context
            and not complete_request
            and not fixed
            and previous in {"average", "total"}
            else None
        )
        if expected_cue and intent.value != expected_cue:
            raise SemanticError(
                "invalid_plan", "Planned aggregation contradicts the current request"
            )
        if not cue and inherited and intent.value != inherited:
            raise SemanticError(
                "invalid_plan", "Planned aggregation contradicts the previous analysis"
            )
        source = (
            "metric_convention"
            if expected_cue and expected_cue != cue
            else "explicit"
            if expected_cue
            else "inherited"
            if inherited
            else "metric_convention"
            if fixed
            else "planner_selected"
        )
        if open_start and (
            plan["status"] != "query"
            or query.get("player_name") not in current_names
            or query.get("metric") != "pts"
            or query.get("operation", "summary") != "summary"
            or query.get("window") != "date_range"
            or query.get("start_date") != open_start
            or len(seasons) > 1
            or query.get("season") != seasons[0]
            or query.get("season_type", phase) != phase
            or intent.value != "average"
            or any(
                query.get(k) is not None
                for k in ("team_abbr", "opponent_abbr", "min_games", "min_attempts")
            )
            or query.get("home_away") != current_scope.get("home_away")
        ):
            raise SemanticError(
                "clarification_required",
                "The existing since-month summary requires matching start scope and per-game aggregation; specify a closed interval or explicit aggregation otherwise",
            )
        if open_start:
            source = "legacy_average_compatibility"
        if eligible and complete:
            expected = dict(
                current_scope,
                season=seasons[0],
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
            plan["validated_points_scope"] = expected
            if not cue and not inherited:
                if intent.value not in {"total", "average", "unspecified"}:
                    raise SemanticError("invalid_plan", "Invalid points aggregation")
                source = "omitted_default"
                policy = {
                    "policy": "dated_points_per_game_v3",
                    "source": "deterministic_product_policy",
                    "reason": "aggregation_omitted",
                    "original_aggregation": intent.value,
                    "model_aggregation_source": intent.source,
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
        if (
            partial_phase in {"Regular Season", "Playoffs", "Both"}
            and query.get("metric") == "pts"
            and query.get("operation", "summary") == "summary"
        ):
            if query.get("season_type", partial_phase) != partial_phase:
                raise SemanticError(
                    "invalid_plan",
                    "Planned phase contradicts the requested or inherited scope",
                )
            query.setdefault("season_type", partial_phase)
        elif eligible and (complete or open_start):
            query.setdefault("season_type", phase)
        if query["aggregation"] == "unspecified":
            raise SemanticError(
                "clarification_required", "Specify an aggregation for this request"
            )
        resolutions.append(
            {
                "source": source,
                "original_aggregation": intent.value,
                "chosen_aggregation": query["aggregation"],
                "model_aggregation_source": intent.source,
            }
        )
        query.pop("aggregation_source", None)
    plan["aggregation_resolution"] = resolutions
    return policy
