"""Conservative standalone grammar. A full match is required; unknown scope defers.

Overview, scorecard and split handlers are resolved by the public host first.
This provider covers scalar summaries, season comparisons and explicit rankings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields, replace
from typing import Any

from app.agent import intelligence_contract as api
from app.agent.catalog import load_semantic_catalog
from app.agent.plan_execution import plan_schema
from app.agent.scope_policy import explicit_aggregation
from app.agent.semantics import GAME_SHARE_PERCENT, SemanticError

_METRICS = {
    "points": "pts",
    "rebounds": "reb",
    "offensive rebounds": "oreb",
    "defensive rebounds": "dreb",
    "personal fouls": "pf",
    "oreb": "oreb",
    "dreb": "dreb",
    "pf": "pf",
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
    "fg%": "fg_pct",
    "ft%": "ft_pct",
    "3p%": "fg3_pct",
}
_METRIC = "|".join(re.escape(k) for k in _METRICS)
_SIMPLE_COUNT_METRIC = "|".join(
    re.escape(k) for k, value in _METRICS.items() if not value.endswith("_pct")
)
_SIMPLE_RATIO_METRIC = "|".join(
    re.escape(k) for k, value in _METRICS.items() if value.endswith("_pct")
)
_PHASE = r"(?P<phase>regular season|playoffs|regular season and playoffs)"
_SEASON = r"(?P<season>20\d{2}-\d{2})"
_PERIOD = rf"(?:in|for|during) (?:the )?{_SEASON} {_PHASE}"
_MODE = r"(?P<mode>per game|total|in total)"
_PHASES = {
    "regular season": "Regular Season",
    "playoffs": "Playoffs",
    "regular season and playoffs": "Both",
}


def followup_metric_key(phrase: str) -> str | None:
    """Resolve an entire metric phrase from the reference grammar or catalog."""
    if phrase in _METRICS:
        return _METRICS[phrase]
    metric = load_semantic_catalog().resolve_metric(phrase)
    return metric.key if metric else None


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
        direction=("lower" if metric == "tov" else "higher") if ranking else None,
        limit=int(bits.get("limit") or 10),
        aggregation_source="explicit" if bits.get("mode") else "planner_selected",
    )


@dataclass(frozen=True, slots=True)
class ReferenceProvider:
    descriptor = api.ProviderDescriptor(
        "reference",
        "1.0.1",
        api.API_VERSION,
        ("nba_semantics/0.1",),
        ("metrics",),
        False,
    )

    def prompts(self):
        return api.PromptBundle("reference/2", None)

    def classify(self, context):
        return api.IntentSelection("metrics", context.prior is not None)

    async def plan(self, context, intent, *, prompts, budget, model):
        from app.agent.derived_series import period_comparison_plan

        periods = period_comparison_plan(
            context.question, context.selected_season, context.player_names
        )
        if periods:
            proposals = []
            scope_fields = {f.name for f in fields(api.Scope)}
            for specification in periods["queries"]:
                query = dict(specification)
                query.pop("playoff_round", None)
                scope = api.Scope(
                    **{key: query.pop(key) for key in scope_fields if key in query}
                )
                proposals.append(
                    api.QueryProposal(
                        scope=scope, aggregation_source="explicit", **query
                    )
                )
            return api.QueryPlan("metrics", "compare", tuple(proposals))
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
        scalar_text = text
        scalar_phase = "regular season"
        phase_suffix = re.search(
            r" \((regular season|playoffs|(?:both )?regular season and playoffs)\)$",
            text,
        )
        if phase_suffix:
            scalar_text = text[: phase_suffix.start()].rstrip("?.")
            scalar_phase = phase_suffix[1].removeprefix("both ")
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
        # An omitted phase on a fully matched season scalar uses the public
        # metric-query default: Regular Season. A whole-string match prevents
        # silently dropping windows, venues, opponents or other modifiers.
        scalar_patterns = (
            rf"(?P<name>.+?) (?P<metric>{_SIMPLE_COUNT_METRIC}) (?P<mode>per game|total|in total) in (?P<season>20\d{{2}}-\d{{2}})",
            rf"(?P<name>.+?) (?P<mode>total) (?P<metric>{_SIMPLE_COUNT_METRIC}) in (?P<season>20\d{{2}}-\d{{2}})",
            rf"(?P<name>.+?) (?P<metric>{_SIMPLE_RATIO_METRIC}) in (?P<season>20\d{{2}}-\d{{2}})",
        )
        scalar = next(
            (
                match
                for pattern in scalar_patterns
                if (match := re.fullmatch(pattern, scalar_text))
            ),
            None,
        )
        if scalar and scalar["name"].casefold() in {
            name.casefold() for name in context.player_names
        }:
            bits = {**scalar.groupdict(), "phase": scalar_phase}
            return api.QueryPlan("metrics", "query", (_query(bits, name=bits["name"]),))
        # A follow-up inherits only the host-authorized successful scope. An
        # explicit new request must match its own complete grammar below.
        follow = re.fullmatch(
            rf"(?:what|how) about (?:(?P<prefix>total) )?(?P<metric>{_METRIC})(?: (?P<mode>per game|in total|total))?(?: instead)?",
            text,
        )
        if (
            follow
            and context.prior
            and len(context.prior.player_names) == 1
            and len(context.prior.scopes) == 1
        ):
            if context.prior.requires_scope_clarification:
                return api.Withheld("clarification_required", "ambiguous_scope")
            metric = _METRICS[follow["metric"]]
            try:
                explicit = explicit_aggregation(text)
            except SemanticError:
                return api.Withheld("clarification_required", "ambiguous_aggregation")
            if metric.endswith("_pct"):
                if explicit is not None:
                    return api.Withheld(
                        "clarification_required", "ambiguous_aggregation"
                    )
                # The proposal's source vocabulary is narrower than the host's
                # final resolution provenance; the host records convention.
                aggregation, source = "ratio", "planner_selected"
            elif explicit is not None:
                aggregation, source = explicit, "explicit"
            elif context.prior.aggregation in ("average", "total"):
                aggregation, source = context.prior.aggregation, "inherited"
            else:
                # A legacy or multi-query context does not prove the prior
                # aggregation. Never manufacture a per-game follow-up.
                return api.Withheld("clarification_required", "ambiguous_aggregation")
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
                        query,
                        aggregation=aggregation,
                        aggregation_source=source,
                        min_games=context.prior.min_games,
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
            rf"(?:show |what (?:is|was) )?(?P<name>.+?)(?:'s)? (?P<mode>total) (?P<metric>{_METRIC}) {_PERIOD}",
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
    "three pointers made": "fg3m",
    "3pts": "fg3m",
    "3pm": "fg3m",
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

LEADER_GAME_SHARE = GAME_SHARE_PERCENT / 100


# Match the ranking intent, statistic, and scope independently. Requiring the
# full question prevents a team, efficiency measure or time window disappearing.
_LEADER_METRIC = "|".join(
    re.escape(key).replace(r"\ ", "[ -]") for key in LEADER_METRICS
)
_LEADER_SUBJECT = r"(?:who|which player)"
_LEADER_PERIOD = (
    r"(?: (?:(?:(?:in|during|for) )?(?:the )?(?P<season>20\d{2}-\d{2})(?: season)?"
    r"|this season|(?:right )?now|currently))?"
    r"(?: (?:in )?(?:the )?(?P<phase>regular season|playoffs))?"
)
_LEADER_RATE = r"(?:per game|a game|a night)"
_LEADER_LEAGUE = r"(?: in the (?:league|nba))?"
_LEADER_FORMS = (
    # “Leading assists”, “leads in assists” and “leads the NBA in assists”.
    rf"{_LEADER_SUBJECT}(?: (?:led|leads|was leading|is leading)|'s leading) "
    rf"(?:(?:the (?:league|nba) (?:in|is)|in) )?(?P<metric>{_LEADER_METRIC})"
    rf"(?: {_LEADER_RATE})?{_LEADER_LEAGUE}",
    # “Most” is unambiguously a per-game ranking only with a stated rate.
    rf"{_LEADER_SUBJECT}(?: has| hands out|(?: is|'s) (?:averaging|putting up|handing out)) "
    rf"(?:the )?most (?P<metric>{_LEADER_METRIC}) {_LEADER_RATE}{_LEADER_LEAGUE}",
    rf"{_LEADER_SUBJECT}(?: is|'s) (?:the )?(?:nba(?:'s)? )?"
    rf"(?:(?:best|top|leading) scorer|highest[ -]scoring player|scoring leader){_LEADER_LEAGUE}",
)
_LEADER_PATTERNS = tuple(re.compile(form + _LEADER_PERIOD) for form in _LEADER_FORMS)


def league_leader_plan(question: str, selected_season: str) -> dict[str, Any] | None:
    """Recognize unfiltered league leader questions, including scorer shorthand.

    Per-game leadership is the league's standard for counting stats. Anything
    beyond a season and phase defers to the model planner.
    """
    text = " ".join(question.strip().rstrip("?.!").replace("’", "'").casefold().split())
    match = next(
        (m for pattern in _LEADER_PATTERNS if (m := pattern.fullmatch(text))), None
    )
    if not match:
        return None
    season = match["season"] or selected_season
    if season != selected_season:
        return None
    phase = (
        "Playoffs" if (match["phase"] or "").lower() == "playoffs" else "Regular Season"
    )
    metric_name = match.groupdict().get("metric", "scoring")
    metric = LEADER_METRICS[re.sub(r"[ -]", " ", metric_name.lower())]
    regular = phase == "Regular Season"
    return {
        "status": "query",
        "model_calls": 0,
        "message": f"League leaders in {LEADER_LABELS[metric]} per game, {season} {phase.lower()}"
        + (
            "; ranked players appeared in at least 65% of their team's games."
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


def conversation_leader_plan(question, context, selected_season):
    """Resolve a complete leaderboard intent against the last successful population."""
    from app.agent.game_filters import filter_scope
    from app.agent.semantics import PLAYOFF_ROUND_LABELS
    from app.seasons import SEASONS

    scope = context.get("scope") or {}
    league = re.search(r"\b(?:league|nba)\b", question, re.I)
    period = re.search(
        r"\b(?:now|currently|this season|20\d{2}-\d{2})\b", question, re.I
    )
    named = re.search(r"\b20\d{2}-\d{2}\b", question)
    season = named[0] if named else selected_season
    if scope and not league and not period:
        season = scope.get("season") or season
    plan = league_leader_plan(question, season)
    if not plan or season not in SEASONS or league or not scope:
        return plan if season in SEASONS else None

    filters = filter_scope(scope)
    team = filters.pop("team_abbr", None)
    previous_phases = scope.get("phases") or [scope.get("season_type")]
    prior_round = scope.get("playoff_round")
    has_window = scope.get("window") not in (None, "season_to_date") or scope.get(
        "start"
    )
    if filters or (
        not period
        and (has_window or len(previous_phases) > 1 or "Both" in previous_phases)
    ):
        return {
            "status": "clarification_required",
            "queries": [],
            "model_calls": 0,
            "message": f"The previous answer used a narrower sample. For the full league, ask “Who leads the league in {LEADER_LABELS[plan['queries'][0]['metric']]} this season?”",
        }
    if (
        not period
        and not re.search(r"\b(?:playoffs|regular season)\b", question, re.I)
        and prior_round is not None
        and (prior_round not in PLAYOFF_ROUND_LABELS or previous_phases != ["Playoffs"])
    ):
        return {
            "status": "clarification_required",
            "queries": [],
            "model_calls": 0,
            "message": "Select a canonical playoff round within Playoffs scope.",
        }
    if (
        not period
        and previous_phases == ["Playoffs"]
        and not re.search(r"\b(?:playoffs|regular season)\b", question, re.I)
    ):
        plan = league_leader_plan(question.rstrip("?.! ") + " in the playoffs", season)
        if plan and prior_round is not None:
            plan["queries"][0]["playoff_round"] = prior_round
            metric = plan["queries"][0]["metric"]
            plan["message"] = (
                f"League leaders in {LEADER_LABELS[metric]} per game, "
                f"{season} {PLAYOFF_ROUND_LABELS[prior_round]}."
            )
    if team:
        plan["queries"][0]["team_abbr"] = team
        round_key = plan["queries"][0].get("playoff_round")
        plan["message"] = f"Ranking qualifying players for {team}" + (
            f" in the {PLAYOFF_ROUND_LABELS[round_key]}." if round_key else "."
        )
    return plan
