"""Bounded, source-derived conversation state; never a substitute for evidence."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Any

from app.agent import intelligence_contract as api
from app.seasons import SEASONS, season_bounds

_QUERY_SCOPE_FIELDS = (
    "metric",
    "season",
    "season_type",
    "aggregation",
    "operation",
    "player_id",
    "window",
    "n",
    "start_date",
    "as_of",
    "team_abbr",
    "opponent_abbr",
    "home_away",
    "game_result",
    "weekdays",
    "stat_filters",
    "min_games",
    "min_attempts",
    "direction",
    "limit",
    "seasons",
    "excluded_player_ids",
)


def _scope_fingerprint(scope: Any) -> str | None:
    """Bind every effective scope field without copying it into the prior hint."""
    if not isinstance(scope, dict):
        return None
    try:
        serialized = json.dumps(
            scope, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(serialized).hexdigest()


def successful_analysis(payload: dict[str, Any]) -> bool:
    # Early availability answers predate the shared status field.
    return payload.get("status") == "ok" or (
        payload.get("status") is None
        and bool(payload.get("availability_scope"))
        and bool(payload.get("availability_evidence"))
    )


def analysis_context(question: str, payload: dict[str, Any]) -> dict[str, Any]:
    context = _analysis_context(question, payload)
    # Reconstruct this hint from the host's resolved query and evidence. Saved
    # conversation_context alone is never authority for planner provenance.
    context.pop("validated_summary", None)
    if validated := _validated_summary(payload, context):
        context["validated_summary"] = validated
    # The route label is stored for later routing; it is never planner evidence.
    route = (payload.get("ask_route") or {}).get("route")
    if context and route:
        context["route"] = route
    return context


def _validated_summary(
    payload: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any] | None:
    """Expose one executed summary's aggregation, never an authored plan alone."""
    if payload.get("status") != "ok":
        return None
    plan = payload.get("query_plan")
    evidence = payload.get("semantic_evidence")
    if not isinstance(plan, dict) or not isinstance(evidence, dict):
        return None
    queries, resolutions = plan.get("queries"), plan.get("aggregation_resolution")
    rows, scope, metric = (
        evidence.get("rows"),
        evidence.get("scope"),
        evidence.get("metric"),
    )
    players = context.get("players")
    if not (
        plan.get("route") == "governed_metrics"
        and isinstance(queries, list)
        and len(queries) == 1
        and isinstance(queries[0], dict)
        and isinstance(resolutions, list)
        and len(resolutions) == 1
        and isinstance(resolutions[0], dict)
        and isinstance(rows, list)
        and len(rows) == 1
        and isinstance(rows[0], dict)
        and isinstance(scope, dict)
        and isinstance(metric, dict)
        and isinstance(players, list)
        and len(players) == 1
        and isinstance(players[0], dict)
    ):
        return None
    query, resolution = queries[0], resolutions[0]
    aggregation, source = query.get("aggregation"), resolution.get("source")
    fingerprint = _scope_fingerprint(scope)
    if (
        query.get("operation") != "summary"
        or type(aggregation) is not str
        or aggregation not in api.PRIOR_AGGREGATIONS
        or type(source) is not str
        or source not in api.PRIOR_AGGREGATION_SOURCES
        or resolution.get("chosen_aggregation") != aggregation
        or type(query.get("player_id")) is not int
        or not isinstance(query.get("metric"), str)
        or not isinstance(query.get("season"), str)
        or type(query.get("season_type")) is not str
        or query.get("season_type") not in {"Regular Season", "Playoffs", "Both"}
        or query["player_id"] != players[0].get("player_id")
        or query["player_id"] != rows[0].get("player_id")
        or metric.get("key") != query["metric"]
        or context.get("metrics") != [query["metric"]]
        or context.get("scope") != scope
        or fingerprint is None
        or any(
            scope.get(key) != query.get(key)
            for key in _QUERY_SCOPE_FIELDS
            if key != "as_of" or query.get("as_of") is not None
        )
    ):
        return None
    return {
        "route": "governed_metrics",
        "aggregation": aggregation,
        "aggregation_source": source,
        "operation": "summary",
        "player_id": query["player_id"],
        "metric": query["metric"],
        "season": query["season"],
        "season_type": query["season_type"],
        "scope_fingerprint": fingerprint,
    }


def _analysis_context(question: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not successful_analysis(payload):
        return {}
    if payload.get("conversation_context"):
        return dict(payload["conversation_context"], question=question)
    evidence = payload.get("semantic_evidence") or {}
    profiles = payload.get("player_profiles") or [
        payload.get("player_profile") or payload.get("reference_player")
    ]
    players = [
        {key: profile["player"][key] for key in ("player_id", "player_name")}
        for profile in profiles
        if profile
        and all(
            key in profile.get("player", {}) for key in ("player_id", "player_name")
        )
    ]
    context = {
        "question": question,
        "answer_summary": str(payload.get("answer", ""))[:1500],
        "players": players,
        "scope": evidence.get("scope") or {},
        "metrics": (
            [
                m["key"]
                for m in evidence.get("metrics", [])
                if isinstance(m, dict) and "key" in m
            ]
            or (
                [evidence["metric"]["key"]]
                if evidence.get("metric", {}).get("key")
                else []
            )
        ),
    }
    if payload.get("availability_scope"):
        request = payload["availability_scope"]
        context.update(
            analysis_type="availability",
            availability_scope=request,
            scope={
                "season": request["season"],
                "start": request.get("start"),
                "end": request.get("end"),
                "phases": [request["phase"]],
            },
            metrics=request["metrics"],
        )
    return context


def hydrate_availability_context(context, source_path):
    """Recover legacy pair identities from validated source IDs, never prose."""
    request = context.get("availability_scope")
    if not request:
        return context
    from app.agent.semantic_serving import source_players
    from app.agent.semantics import SemanticError
    from app.availability import load_availability

    players = source_players(load_availability(source_path)[1])
    recovered = []
    for key, role in (("player_id", "focal"), ("teammate_id", "teammate")):
        player = next((p for p in players if p["player_id"] == request[key]), None)
        if not player:
            raise SemanticError(
                "invalid_context", "Saved players are absent from this source."
            )
        recovered.append(
            {
                "player_id": player["player_id"],
                "player_name": player["player_name"],
                "role": role,
            }
        )
    for hint in context.get("players", []):
        if not any(
            p["player_id"] == hint["player_id"]
            and p["player_name"] == hint["player_name"]
            for p in recovered
        ):
            raise SemanticError(
                "invalid_context", "Saved player identity does not match this source."
            )
    return dict(context, players=recovered)


def resolve_followup(
    question: str, context: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Inherit identity and scope only when the new question does not replace them."""
    if not context:
        return question, {}
    continues_analysis = inherits_analysis(question)
    players = context.get("players", [])
    resolved = question
    if len(players) == 2 and continues_analysis:
        resolved = re.sub(
            r"\b(?:each player|both players|both of them|these two|they|them|their)\b(?!\s+team\s+(?:won|lost)\b)",
            lambda m: (
                " and ".join(p["player_name"] for p in players)
                + ("’s" if m[0].lower() == "their" else "")
            ),
            resolved,
            flags=re.I,
        )
    for player in players:
        name = player["player_name"]
        surname = name.split()[-1]
        if (
            sum(
                p["player_name"].split()[-1].casefold() == surname.casefold()
                for p in players
            )
            == 1
        ):
            # Do not rewrite an already explicit full name.
            if name.casefold() not in resolved.casefold():
                resolved = re.sub(
                    r"(^|\b(?:besides?|except|excluding|other than|about|for|does|is|did|compare)\s+)"
                    + re.escape(surname)
                    + r"(?!\w)",
                    lambda match: match[1] + name,
                    resolved,
                    flags=re.I,
                )
    if len(players) == 1 and continues_analysis:
        resolved = re.sub(
            r"\b(his|him|he)\b(?!\s+team\s+(?:won|lost)\b)",
            lambda m: (
                players[0]["player_name"] + ("’s" if m[0].lower() == "his" else "")
            ),
            resolved,
            flags=re.I,
        )
    scope = context.get("scope", {})
    from app.agent.game_filters import (
        extract_game_filters,
        filter_question,
        filter_scope,
    )

    # Preserve each prior predicate unless this turn explicitly replaces that
    # dimension. Canonical clauses are parsed and validated again on execution.
    try:
        new_predicates = extract_game_filters(question).query_fields
    except ValueError:
        return resolved, {}  # The request's normal scope guard supplies refusal.
    carried = {
        key: value
        for key, value in filter_scope(scope).items()
        if key not in new_predicates and continues_analysis
    }
    if carried:
        resolved += " " + filter_question(carried)
    inherited = {}
    changes_dates = re.search(
        r"\b(?:20\d{2}|last|past|previous|prior|this (?:season|month|week|year)|current season|recently|lately|today|yesterday|since|before|after|from|as.of|January|February|March|April|May|June|July|August|September|October|November|December)\b",
        question,
        re.I,
    )
    if not changes_dates:
        start = scope.get("start") or scope.get("start_date")
        end = scope.get("end") or scope.get("as_of")
        if start and end:
            resolved += f" from {start} through {end}"
            inherited["seasons"] = scope.get("seasons") or [scope.get("season")]
            inherited["seasons"] = [
                season
                for season in inherited["seasons"]
                if season
                and season_bounds(season)[0] <= date.fromisoformat(end)
                and season_bounds(season)[1] >= date.fromisoformat(start)
            ]
    if not re.search(r"\b(?:playoffs?|postseason|regular[ -]season)\b", question, re.I):
        phases = scope.get("phases") or (
            [scope["season_type"]] if scope.get("season_type") else []
        )
        if phases:
            resolved += (
                " ("
                + (
                    "both regular season and playoffs"
                    if len(phases) > 1 or phases == ["Both"]
                    else phases[0]
                )
                + ")"
            )
    dates = re.search(
        r"\bfrom (\d{4}-\d{2}-\d{2}) (?:through|to) (\d{4}-\d{2}-\d{2})\b",
        resolved,
        re.I,
    )
    if dates:
        try:
            start, end = (date.fromisoformat(value) for value in dates.groups())
            inherited["seasons"] = sorted(
                s
                for s in SEASONS
                if season_bounds(s)[0] <= end and season_bounds(s)[1] >= start
            )
        except ValueError:
            pass  # The normal scope validator supplies the user-facing error.
    excluded = []
    for player in players:
        if re.search(
            r"\b(?:besides?|except|excluding|other than)\s+"
            + re.escape(player["player_name"])
            + r"\b",
            resolved,
            re.I,
        ):
            excluded.append(player["player_id"])
    if excluded:
        inherited["excluded_player_ids"] = excluded
    if (
        re.search(r"\bplaymaking\b", question, re.I)
        and "ast" in context.get("metrics", [])
        and not re.search(r"\b(?:turnovers?|ratio|total|potential)\b", question, re.I)
    ):
        resolved += ". Use assists per game as the playmaking measure."
        inherited["assumption"] = (
            "Playmaking is measured by assists per game, following the prior overview."
        )
    return resolved, inherited


def inherits_analysis(question: str) -> bool:
    """Recognize a scope/metric continuation, never a complete new named ask."""
    from app.agent.game_filters import extract_game_filters

    try:
        analysis_question = extract_game_filters(question).cleaned_question
    except ValueError:
        return False  # The normal request guard supplies the scope refusal.
    return bool(
        re.search(
            r"\b(?:each player|both players|both of them|these two|his|him|he|her|she|their|them|they|same|instead)\b",
            analysis_question,
            re.I,
        )
        or re.match(
            r"\s*(?:(?:what|how) about\b|and\b)",
            analysis_question,
            re.I,
        )
        or re.fullmatch(
            r"\s*(?:at home|on the road|in (?:wins|losses))\s*[?.!]*", question, re.I
        )
    )
