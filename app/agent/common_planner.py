"""Conservative common-query recognition: consume the entire question or defer.

Names still pass the governed resolver. Unknown modifiers and conversational
state defer to the established planner; no substring recognition authorizes scope.
"""

from __future__ import annotations

import re
from typing import Any

from app.agent.execution import timed
from app.agent.semantics import Query, SemanticError, load_contract, resolve_entity
from app.seasons import validate_season

COUNTS = {
    "points": "pts",
    "rebounds": "reb",
    "assists": "ast",
    "steals": "stl",
    "blocks": "blk",
    "turnovers": "tov",
    "three pointers": "fg3m",
    "minutes": "min",
}
METRICS = "|".join(re.escape(k) for k in COUNTS)
SEASON = r"(?P<season>20\d{2}-\d{2})"
PHASE = r"(?P<phase>regular season|playoffs|regular season and playoffs)"


@timed("deterministic_planning")
def common_plan(
    question: str, *, players, conversation_context=None
) -> dict[str, Any] | None:
    if conversation_context:
        return None
    text = " ".join(question.strip().rstrip("?").casefold().split())
    scope = rf" in (?:the )?{SEASON} {PHASE}"
    patterns = [
        rf"(?P<name>(?!(?:what|how)\b).+?) (?P<metric>{METRICS}) (?P<mode>per game|total){scope}",
        rf"what (?:is|was) (?P<name>.+?)\'s (?P<metric>{METRICS}) (?P<mode>per game|total){scope}",
        rf"how many (?P<metric>{METRICS}) did (?P<name>.+?) (?P<mode>average|score in total){scope}",
    ]
    match = next((m for p in patterns if (m := re.fullmatch(p, text))), None)
    ranking = False
    if match is None:
        match = re.fullmatch(
            rf"top (?P<limit>\d+) players by (?P<metric>{METRICS}) (?P<mode>per game|total){scope} with at least (?P<minimum>\d+) games",
            text,
        )
        ranking = match is not None
    if match is None:
        return None
    bits = match.groupdict()
    contract = load_contract()
    args: dict[str, Any] = dict(
        metric=COUNTS[bits["metric"]],
        aggregation="average" if bits["mode"] in ("per game", "average") else "total",
        season=bits["season"],
        season_type={
            "regular season": "Regular Season",
            "playoffs": "Playoffs",
            "regular season and playoffs": "Both",
        }[bits["phase"]],
        operation="rank" if ranking else "summary",
    )
    try:
        validate_season(args["season"])
        if ranking:
            args.update(
                min_games=int(bits["minimum"]),
                limit=int(bits["limit"]),
                direction=contract.metric(args["metric"]).direction,
            )
            Query(**args).validate(contract.metric(args["metric"]))
        else:
            resolution = resolve_entity(bits["name"], players)
            if resolution["status"] == "not_found":
                return None
            if resolution["status"] != "ok":
                return dict(
                    status="clarification_required",
                    queries=[],
                    message="Choose the intended player.",
                    identity=resolution,
                    model_calls=0,
                    planner_source="deterministic_common/1",
                )
            player = resolution["matches"][0]
            Query(**args, player_id=player["player_id"]).validate(
                contract.metric(args["metric"])
            )
            args["player_name"] = player["player_name"]
    except (SemanticError, ValueError, TypeError):
        return None
    plan = dict(
        status="query",
        queries=[args],
        message="",
        model_calls=0,
        planner_source="deterministic_common/1",
    )
    from app.agent.semantic_intent import resolve_aggregation_intent

    args["aggregation_source"] = "explicit"
    replacements = {} if ranking else {bits["name"]: "recognized_player"}
    references = {} if ranking else {"recognized_player": args["player_name"]}
    policy = resolve_aggregation_intent(
        question, plan, replacements, references, args["season"], {}, True
    )
    if policy:
        plan["aggregation_policy"] = policy
    return plan
