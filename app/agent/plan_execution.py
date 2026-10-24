"""Public plan schema, narrow normalizers and governed execution. No planning."""

from __future__ import annotations

import copy
import math
import re
from dataclasses import asdict
from typing import Any, Mapping, Sequence

from app.agent.execution import timed
from app.agent.semantics import (
    Evidence,
    Query,
    SemanticError,
    compare_queries,
    load_contract,
    resolve_entity,
    run_query,
)


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def plan_schema() -> dict[str, Any]:
    fields: dict[str, Any] = {
        k: {"type": ["string", "null"]}
        for k in (
            "as_of",
            "start_date",
            "player_name",
            "team_abbr",
            "opponent_abbr",
            "home_away",
            "direction",
        )
    }
    fields.update(
        {k: {"type": ["integer", "null"]} for k in ("n", "min_games", "min_attempts")}
    )
    fields.update(
        {
            k: {"type": "string"}
            for k in (
                "metric",
                "season",
                "season_type",
                "aggregation",
                "operation",
                "window",
            )
        }
    )
    fields["limit"] = {"type": "integer", "minimum": 1, "maximum": 100}
    for field, values in {
        "window": [
            "season_to_date",
            "last_n_games",
            "prior_n_games",
            "last_n_days",
            "last_n_months",
            "last_week",
            "last_month",
            "date_range",
        ],
        "operation": ["summary", "rank", "percentile", "game_log"],
        "aggregation": ["average", "total", "ratio", "unspecified"],
        "season_type": ["Regular Season", "Playoffs", "Both"],
    }.items():
        fields[field]["enum"] = values
    fields["aggregation_source"] = {
        "type": "string",
        "enum": ["explicit", "inherited", "planner_selected", "omitted"],
    }
    fields["direction"]["enum"] = ["higher", "lower", None]
    fields["home_away"]["enum"] = ["HOME", "AWAY", None]

    return _object(
        {
            "status": {
                "type": "string",
                "enum": ["query", "compare", "clarification_required", "unsupported"],
            },
            "queries": {"type": "array", "items": _object(fields)},
            "message": {"type": "string"},
        }
    )


def response_schema(schema: dict[str, Any]) -> dict[str, Any]:
    variants = []
    for status, count in (
        ("query", 1),
        ("compare", 2),
        ("clarification_required", 0),
        ("unsupported", 0),
    ):
        variant = copy.deepcopy(schema)
        variant["properties"]["status"]["enum"] = [status]
        variant["properties"]["queries"].update(minItems=count, maxItems=count)
        variants.append(variant)
    return _object({"plan": {"anyOf": variants}})


def normalize_duplicate_rank_plan(plan):
    """Collapse only identical raw rank specifications; retain auditable plan shape."""
    queries = plan.get("queries", [])
    if (
        plan.get("status") != "compare"
        or not isinstance(queries, list)
        or len(queries) != 2
        or not isinstance(queries[0], dict)
        or queries[0] != queries[1]
        or queries[0].get("operation") != "rank"
    ):
        return plan
    resolutions = plan.get("aggregation_resolution", [])
    if resolutions and (len(resolutions) != 2 or resolutions[0] != resolutions[1]):
        return plan
    normalized = {**plan, "status": "query", "queries": [dict(queries[0])]}
    if resolutions:
        normalized["aggregation_resolution"] = resolutions[:1]
    return normalized


def leader_game_floor(appearances, args, share):
    """Share of the most games any team has in scope (70% of 82 is 58)."""
    games: dict[str, set[str]] = {}
    for row in appearances:
        if (
            row["season"] == args["season"]
            and row["season_type"] == args["season_type"]
        ):
            games.setdefault(str(row.get("team_abbr")), set()).add(row["game_id"])
    most = max((len(ids) for ids in games.values()), default=0)
    return max(1, math.ceil(share * most))


def single_period_rank_plan(plan, question, selected_season):
    """A one-period leaderboard question never becomes a season-vs-season compare.

    Only two same-metric rankings that differ by season alone collapse, and only
    when the question requests only the selected season and has no comparison
    wording. Explicit multiple seasons must never be silently discarded.
    """
    from app.agent.semantic_serving import requested_seasons

    queries = plan.get("queries", [])
    if (
        plan.get("status") != "compare"
        or len(queries) != 2
        or not all(
            isinstance(q, dict) and q.get("operation") == "rank" for q in queries
        )
        or requested_seasons(question, selected_season) != [selected_season]
        or re.search(
            r"\b(?:vs\.?|versus|compar\w*|than|change[ds]?|improv\w*|differen\w*|between|from\b.+\bto)\b",
            question,
            re.I,
        )
    ):
        return plan
    differing = {
        k
        for k in set(queries[0]) | set(queries[1])
        if queries[0].get(k) != queries[1].get(k)
    }
    kept = [i for i, q in enumerate(queries) if q.get("season") == selected_season]
    if differing != {"season"} or len(kept) != 1:
        return plan
    collapsed = {**plan, "status": "query", "queries": [dict(queries[kept[0]])]}
    resolutions = plan.get("aggregation_resolution")
    if isinstance(resolutions, list) and len(resolutions) == 2:
        collapsed["aggregation_resolution"] = [resolutions[kept[0]]]
    return collapsed


@timed("calculation")
def execute_plan(
    plan: Mapping[str, Any],
    evidence: Evidence | None,
    players: Sequence[Mapping[str, Any]],
    *,
    shadow=None,
    query_runner=None,
    appearances=None,
) -> dict[str, Any]:
    if plan["status"] in ("clarification_required", "unsupported"):
        return {
            "status": plan["status"],
            "message": plan["message"],
            "evidence": None,
            **({"identity": plan["identity"]} if "identity" in plan else {}),
        }
    queries = []
    try:
        for specification in plan["queries"]:
            args = dict(specification)
            name = args.pop("player_name", None)
            # Model-supplied IDs must never bypass the identity resolver.
            if "player_id" in args:
                raise SemanticError(
                    "invalid_plan", "Use player names, not generated IDs"
                )
            if name:
                resolution = resolve_entity(name, players)
                if resolution["status"] != "ok":
                    return {
                        "status": "clarification_required",
                        "identity": resolution,
                        "evidence": None,
                    }
                args["player_id"] = resolution["matches"][0]["player_id"]
            elif args.get("operation") in ("summary", "game_log"):
                raise SemanticError("clarification_required", "Specify the player")
            metric = load_contract().metric(args["metric"])
            args["metric"] = metric.key
            if plan.get("leader_game_share"):
                # Serving supplies validated publication appearances instead of
                # preloading facts. Both paths must use the same qualification.
                qualification_rows = (
                    evidence.rows if evidence is not None else appearances
                )
                if qualification_rows is None:
                    raise SemanticError(
                        "invalid_evidence",
                        "Leader qualification requires appearance evidence",
                    )
                args["min_games"] = leader_game_floor(
                    qualification_rows, args, plan["leader_game_share"]
                )
            query = Query(**args)
            if (
                query.operation in ("rank", "percentile")
                and metric.denominator
                and query.min_attempts is None
            ):
                raise SemanticError(
                    "clarification_required", "Specify an attempt threshold"
                )
            query.validate(metric)
            queries.append(query)
        if plan["status"] == "query" and len(queries) == 1:
            if query_runner is not None:
                try:
                    result = query_runner(queries[0])
                except (KeyError, TypeError) as exc:
                    raise SemanticError(
                        "invalid_evidence", "Invalid serving execution"
                    ) from exc
            else:
                assert evidence is not None
                result = run_query(evidence, queries[0])
            if shadow is not None:
                shadow.compare(queries[0], result)
        elif plan["status"] == "compare" and len(queries) == 2:
            assert evidence is not None
            result = compare_queries(evidence, *queries)
        else:
            raise SemanticError("invalid_plan", "Invalid query count/status")
    except SemanticError as exc:
        return {"status": exc.code, "message": str(exc), "evidence": None}
    except (TypeError, KeyError) as exc:
        raise SemanticError("invalid_plan", "Unexpected query fields") from exc
    return {
        "status": result["status"],
        "resolved_queries": [asdict(q) for q in queries],
        "evidence": result,
        "aggregation_resolution": plan.get("aggregation_resolution", []),
        **(
            {"aggregation_policy": plan["aggregation_policy"]}
            if "aggregation_policy" in plan
            else {}
        ),
    }
