"""Explicit scoring summaries within captured ESPN games, without league ranks."""

import math
import re
from collections import defaultdict
from dataclasses import replace

from app.agent.payload import answer_payload
from app.agent.route_labels import route_hint
from app.agent.semantics import Query, SemanticError, run_query


def scoring_request(question, season):
    """Small whole-question grammar; never discard a filter or historical scope."""
    text = " ".join(question.casefold().strip().rstrip("?.!").split())
    period = rf"(?:this season|(?:in |for )?(?:the )?{re.escape(season)}(?: regular season)?)"
    match = re.fullmatch(
        r"(?:(?:who (?:are|is)|what (?:are|is)|show(?: me)?|give me) (?:the )?)?"
        r"(?:(?:top|leading) (?:(?P<count>\d+) )?(?:scorers?|scores?)|scoring leaders)"
        r"(?: (?:by|in) (?P<metric>points per game|ppg|total points))?"
        r"(?P<captured> in (?:the )?captured espn games)? " + period,
        text,
    )
    if not match:
        return None
    count = int(match["count"] or 10)
    if not 1 <= count <= 100:
        raise SemanticError(
            "invalid_scope", "Choose between one and one hundred players."
        )
    return dict(
        limit=count,
        aggregation="total"
        if match["metric"] == "total points"
        else "average"
        if match["metric"]
        else None,
        captured=bool(match["captured"]),
    )


def scoring_summary(evidence, season, aggregation, limit):
    """Reuse governed individual summaries; sort only within validated captures.

    League rank/percentile queries still fail for this evidence. Every returned
    value, component and game list belongs to an ordinary individual summary.
    """
    evidence.validate()
    if evidence.cohort_complete or evidence.covered_scopes != frozenset(
        {(season, "Regular Season")}
    ):
        raise SemanticError(
            "unsupported_coverage",
            "Captured scoring requires one partial regular-season scope.",
        )
    teams, grouped = defaultdict(set), defaultdict(list)
    for row in evidence.rows:
        teams[row["team_abbr"]].add(row["game_id"])
        grouped[row["player_id"]].append(row)
    if not grouped:
        raise SemanticError(
            "unavailable", "No captured scoring appearances are available."
        )
    minimum = (
        max(1, math.ceil(0.7 * max(map(len, teams.values()), default=0)))
        if aggregation == "average"
        else 1
    )
    query = Query(
        metric="pts",
        season=season,
        aggregation=aggregation,
        operation="summary",
        as_of=max(str(row["game_date"]) for row in evidence.rows),
        min_games=minimum,
        limit=limit,
    )
    results = []
    for pid, rows in grouped.items():
        # Scoped individual evidence retains the original capture provenance.
        result = run_query(replace(evidence, rows=rows), replace(query, player_id=pid))
        results.append(result["rows"][0])
    eligible = [r for r in results if r["eligible"]]
    eligible.sort(key=lambda r: (-r["value"], r["player_id"]))
    result["scope"].update(player_id=None, limit=limit)
    result["rows"] = eligible[:limit]
    result["status"] = "ok" if result["rows"] else "empty_cohort"
    result["cohort"].update(
        size=len(eligible),
        excluded_players=len(results) - len(eligible),
        qualification_basis="captured_team_game_share"
        if aggregation == "average"
        else "recorded_appearance",
    )
    result["captured_scoring"] = dict(
        coverage="captured_games_only",
        ordering="value_descending_then_player_id",
        league_rank_available=False,
    )
    return result


def answer_captured_scoring(question, warehouse, season, render, conversation_id=None):
    request = scoring_request(question, season)
    if request is None:
        return None
    snapshot, evidence = warehouse.load([season])
    if (
        evidence.cohort_complete
        or snapshot.get("review_metadata", {}).get("provider") != "espn"
    ):
        return None
    players = warehouse.players(evidence)
    if {r["player_id"] for r in evidence.rows} != {p["player_id"] for p in players}:
        raise SemanticError(
            "source_policy_denied",
            "Captured scoring requires identities for every player in the selected capture.",
        )
    choices = [
        f"Top {request['limit']} scorers by {metric} in captured ESPN games {season} regular season"
        for metric in ("points per game", "total points")
    ]
    if request["aggregation"] is None or not request["captured"]:
        payload = answer_payload(
            "I can show scoring leaders within our captured ESPN games. These results do not establish the NBA league leader. Choose points per game or total points for that captured-game view.",
            status="clarification_required",
            followups=choices,
        )
        payload["clarification_question"] = question
    else:
        result = scoring_summary(
            evidence, season, request["aggregation"], request["limit"]
        )
        payload = render(
            dict(
                status=result["status"],
                evidence=result,
                resolved_queries=[result["scope"]],
            ),
            players,
        )
        metric = (
            "points per game" if request["aggregation"] == "average" else "total points"
        )
        payload["answer"] = (
            f"Scoring leaders within captured ESPN games, ordered by {metric}. This view does not establish the NBA league leader.\n\n"
            + payload["answer"]
        )
        payload["captured_scoring"] = result["captured_scoring"]
        payload["followups"] = [
            q
            for q in choices
            if ("total points" in q) != (request["aggregation"] == "total")
        ]
        if request["aggregation"] == "average":
            payload["assumptions"].append(
                "The minimum appearance count is based on a share of the busiest captured team’s games; it is a captured-sample qualification."
            )
    payload["assumptions"].extend(snapshot.get("coverage_notes", []))
    payload["source_coverage"] = snapshot["review_metadata"]
    payload["source_query"] = snapshot.get("capture", {})
    payload["semantic_plan"] = dict(
        status=payload["status"],
        model_calls=0,
        kind="captured_scoring",
        aggregation=request["aggregation"],
    )
    payload["agent_plan"] = dict(
        route="governed_metrics",
        confidence=1.0,
        needs_clarification=payload["status"] == "clarification_required",
    )
    payload["conversation_id"] = conversation_id
    payload["tool_calls"] = [
        dict(
            name="governed_metrics",
            args={"seasons": [season]},
            status=payload["status"],
            result={"source_query": payload["source_query"]},
        )
    ]
    return route_hint(
        payload,
        route="governed_metrics",
        source="deterministic",
        reason_code="captured_scoring",
    )
