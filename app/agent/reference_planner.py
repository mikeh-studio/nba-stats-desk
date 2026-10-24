"""Conservative standalone grammar. A full match is required; unknown scope defers.

Overview, scorecard and split handlers are resolved by the public host first.
This provider covers scalar summaries, season comparisons and explicit rankings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields, replace
from typing import Any

from app.agent import intelligence_contract as api
from app.agent.plan_execution import plan_schema

_METRICS = {
    "points": "pts",
    "rebounds": "reb",
    "assists": "ast",
    "steals": "stl",
    "blocks": "blk",
    "turnovers": "tov",
    "minutes": "min",
    "three pointers": "fg3m",
    "field goal percentage": "fg_pct",
    "three point percentage": "fg3_pct",
    "free throw percentage": "ft_pct",
    "true shooting percentage": "ts_pct",
}
_METRIC = "|".join(re.escape(k) for k in _METRICS)
_PHASE = r"(?P<phase>regular season|playoffs|regular season and playoffs)"
_SEASON = r"(?P<season>20\d{2}-\d{2})"
_PERIOD = rf"(?:in|for|during) (?:the )?{_SEASON} {_PHASE}"
_MODE = r"(?P<mode>per game|total|in total)"
_PHASES = {
    "regular season": "Regular Season",
    "playoffs": "Playoffs",
    "regular season and playoffs": "Both",
}


def _query(bits, *, name=None, scope=None, ranking=False):
    metric = _METRICS[bits["metric"]]
    return api.QueryProposal(
        metric=metric,
        scope=scope or api.Scope(bits["season"], _PHASES[bits["phase"]]),
        operation="rank" if ranking else "summary",
        aggregation="ratio"
        if metric.endswith("_pct")
        else "total"
        if bits.get("mode") in ("total", "in total")
        else "average",
        player_name=name,
        min_games=int(bits["minimum"]) if bits.get("minimum") else None,
        direction="lower" if metric == "tov" else "higher" if ranking else None,
        limit=int(bits.get("limit") or 10),
        aggregation_source="explicit" if bits.get("mode") else "planner_selected",
    )


@dataclass(frozen=True, slots=True)
class ReferenceProvider:
    descriptor = api.ProviderDescriptor(
        "reference",
        "1.0.0",
        api.API_VERSION,
        ("nba_semantics/0.1",),
        ("metrics",),
        False,
    )

    def prompts(self):
        return api.PromptBundle("reference/1", None)

    def classify(self, context):
        return api.IntentSelection("metrics", context.prior is not None)

    async def plan(self, context, intent, *, prompts, budget, model):
        leader = league_leader_plan(context.question, context.selected_season)
        if leader:
            q = dict(leader["queries"][0])
            scope_fields = {f.name for f in fields(api.Scope)}
            scope = api.Scope(**{key: q.pop(key) for key in scope_fields if key in q})
            return api.QueryPlan(
                "metrics",
                "query",
                (api.QueryProposal(scope=scope, **q),),
                qualification="league_leader"
                if leader.get("leader_game_share")
                else "explicit_or_default",
            )
        text = " ".join(
            context.question.strip().rstrip("?.").replace("’", "'").casefold().split()
        )
        if context.prior:
            # The public conversation resolver appends the authorized phase. It
            # is accepted only when it agrees with the prior scope.
            phase = (
                context.prior.scopes[0].season_type.casefold()
                if context.prior.scopes
                else ""
            )
            suffix = " (" + phase + ")"
            if text.endswith(suffix):
                text = text[: -len(suffix)].rstrip("?.")
        # A follow-up inherits only the host-authorized successful scope. An
        # explicit new request must match its own complete grammar below.
        follow = re.fullmatch(
            rf"(?:what|how) about (?P<metric>{_METRIC})(?: instead)?", text
        )
        if (
            follow
            and context.prior
            and len(context.prior.player_names) == 1
            and len(context.prior.scopes) == 1
        ):
            query = _query(
                follow.groupdict(),
                name=context.prior.player_names[0],
                scope=context.prior.scopes[0],
            )
            return api.QueryPlan(
                "metrics",
                "query",
                (
                    replace(
                        query, aggregation="average", aggregation_source="inherited"
                    ),
                ),
            )
        ranking = re.fullmatch(
            rf"top (?P<limit>\d+) players by (?P<metric>{_METRIC})(?: {_MODE})? {_PERIOD}(?:,? (?:minimum|with at least) (?P<minimum>\d+) games)?",
            text,
        )
        if ranking:
            bits = ranking.groupdict()
            if _METRICS[bits["metric"]].endswith("_pct"):
                return api.Withheld("clarification_required", "missing_qualification")
            if not bits["mode"]:
                return api.Withheld("clarification_required", "ambiguous_scope")
            return api.QueryPlan("metrics", "query", (_query(bits, ranking=True),))
        comparison = re.fullmatch(
            rf"compare (?P<name>.+?)(?:'s)? (?P<metric>{_METRIC}) {_MODE} {_PERIOD} (?:versus|vs) (?:the )?(?P<baseline>20\d{{2}}-\d{{2}}) (?P<baseline_phase>regular season|playoffs)",
            text,
        )
        if comparison:
            bits = comparison.groupdict()
            query = _query(bits, name=bits["name"])
            baseline = replace(
                query,
                scope=api.Scope(bits["baseline"], _PHASES[bits["baseline_phase"]]),
            )
            return api.QueryPlan("metrics", "compare", (query, baseline))
        patterns = (
            rf"(?:show |what (?:is|was) )?(?P<name>.+?)(?:'s)? (?P<metric>{_METRIC}) {_MODE} {_PERIOD}",
        )
        match = next(
            (m for pattern in patterns if (m := re.fullmatch(pattern, text))), None
        )
        if match:
            bits = match.groupdict()
            return api.QueryPlan("metrics", "query", (_query(bits, name=bits["name"]),))
        return api.Withheld("unsupported", "unsupported_operation")


LEADER_METRICS = {
    "points": "pts",
    "scoring": "pts",
    "rebounds": "reb",
    "rebounding": "reb",
    "assists": "ast",
    "steals": "stl",
    "blocks": "blk",
    "threes": "fg3m",
    "three pointers": "fg3m",
    "3 pointers": "fg3m",
    "turnovers": "tov",
    "minutes": "min",
}

LEADER_LABELS = dict(
    pts="points",
    reb="rebounds",
    ast="assists",
    stl="steals",
    blk="blocks",
    fg3m="three-pointers made",
    tov="turnovers",
    min="minutes",
)

LEADER_GAME_SHARE = 0.7


def league_leader_plan(question: str, selected_season: str) -> dict[str, Any] | None:
    """Recognize the whole of "who led the league in X [season] [phase]".

    Per-game leadership is the league's standard for counting stats. Anything
    beyond a season and phase defers to the model planner.
    """
    metrics = "|".join(
        re.escape(k) for k in sorted(LEADER_METRICS, key=len, reverse=True)
    ).replace(r"\ ", r"[ -]")
    match = re.fullmatch(
        r"(?:who|which player)\s+(?:led|leads|was leading|is leading)\s+the\s+(?:league|nba)"
        rf"\s+(?:in|is)\s+(?P<metric>{metrics})"
        r"(?:\s+(?:in|during|for)?\s*(?:the\s+)?(?P<season>20\d{2}-\d{2})(?:\s+season)?)?"
        r"(?:\s+(?:in\s+)?(?:the\s+)?(?P<phase>regular season|playoffs))?",
        " ".join(question.strip().rstrip("?.!").split()),
        re.I,
    )
    if not match:
        return None
    season = match["season"] or selected_season
    if season != selected_season:
        return None
    phase = (
        "Playoffs" if (match["phase"] or "").lower() == "playoffs" else "Regular Season"
    )
    metric = LEADER_METRICS[re.sub(r"[ -]", " ", match["metric"].lower())]
    regular = phase == "Regular Season"
    return {
        "status": "query",
        "model_calls": 0,
        "message": f"League leaders in {LEADER_LABELS[metric]} per game, {season} {phase.lower()}"
        + (
            "; ranked players appeared in at least 70% of their team's games (the NBA's per-game leader standard)."
            if regular
            else "."
        ),
        **({"leader_game_share": LEADER_GAME_SHARE} if regular else {}),
        "queries": [
            {
                **{
                    key: None
                    for key in plan_schema()["properties"]["queries"]["items"][
                        "properties"
                    ]
                    if key != "aggregation_source"
                },
                "metric": metric,
                "season": season,
                "season_type": phase,
                "aggregation": "average",
                "operation": "rank",
                "window": "season_to_date",
                "player_name": None,
                "direction": "higher",
                "min_games": None,
                "limit": 10,
            }
        ],
    }
