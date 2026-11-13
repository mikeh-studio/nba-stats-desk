"""Bounded, evidence-only appearance series and explicit period comparisons.

Selection and arithmetic use the governed metric engine. Display limits never
change a rolling window or cumulative prefix.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from app.agent.payload import answer_payload
from app.agent.semantics import (
    Evidence,
    Query,
    SemanticError,
    aggregate,
    load_contract,
    resolve_entity,
    select_query_rows,
)

METRICS = {
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
    "three-pointers made": "fg3m",
    "field goal attempts": "fga",
    "three point attempts": "fg3a",
    "free throw attempts": "fta",
    "field goal percentage": "fg_pct",
    "three point percentage": "fg3_pct",
    "free throw percentage": "ft_pct",
    "effective field goal percentage": "efg_pct",
    "true shooting percentage": "ts_pct",
    "fg%": "fg_pct",
    "3p%": "fg3_pct",
    "ft%": "ft_pct",
    "efg%": "efg_pct",
    "ts%": "ts_pct",
}
_METRIC = "|".join(re.escape(key) for key in sorted(METRICS, key=len, reverse=True))
_PHASES = {
    "regular season": "Regular Season",
    "playoffs": "Playoffs",
    "regular season and playoffs": "Both",
}
_PHASE = r"(?:regular season and playoffs|regular season|playoffs)"
_SEASON = r"20\d{2}-\d{2}"


def _text(question: str) -> str:
    return " ".join(question.strip().rstrip(".?!").replace("’", "'").casefold().split())


def explicit_period(phrase: str, selected_season: str) -> dict[str, Any] | None:
    """Parse an entire operand; never discard an unrecognized scope clause."""
    phrase = re.sub(r"^(?:in|for|during|over) (?:the )?", "", phrase.strip())
    season = re.fullmatch(rf"(?P<season>{_SEASON})(?: (?P<phase>{_PHASE}))?", phrase)
    if season:
        return {
            "season": season["season"],
            "season_type": _PHASES.get(season["phase"], "Regular Season"),
        }
    recent = re.fullmatch(
        rf"(?:(?P<prior>prior|previous)|last|past) (?P<n>\d+) (?:games|appearances)(?: (?:in|of) (?:the )?(?P<season>{_SEASON})(?: (?P<phase>{_PHASE}))?)?",
        phrase,
    )
    if recent and 1 <= int(recent["n"]) <= 100:
        return {
            "season": recent["season"] or selected_season,
            "season_type": _PHASES.get(recent["phase"], "Regular Season"),
            "window": "prior_n_games" if recent["prior"] else "last_n_games",
            "n": int(recent["n"]),
        }
    dates = re.fullmatch(
        rf"from (?P<start>\d{{4}}-\d{{2}}-\d{{2}}) (?:through|to) (?P<end>\d{{4}}-\d{{2}}-\d{{2}})(?: (?:in )?(?:the )?(?P<season>{_SEASON})(?: (?P<phase>{_PHASE}))?)?",
        phrase,
    )
    if dates:
        from app.agent.semantic_serving import seasons_in_dates

        seasons = seasons_in_dates(dates["start"], dates["end"])
        # This bounded operand grammar admits a single archive. It must never
        # clip an explicit date interval to whichever season was selected.
        if len(seasons) != 1 or (
            dates["season"] is not None and dates["season"] != seasons[0]
        ):
            return None
        return {
            "season": seasons[0],
            "season_type": _PHASES.get(dates["phase"], "Regular Season"),
            "window": "date_range",
            "start_date": dates["start"],
            "as_of": dates["end"],
        }
    return None


@dataclass(frozen=True)
class DerivedSeriesPlan:
    query: dict[str, Any]
    mode: str
    rolling_n: int | None = None


def derived_series_followup_plan(
    question: str, context: Mapping[str, Any]
) -> DerivedSeriesPlan | None:
    """Change one measure inside a successful host-retained series scope."""
    if context.get("analysis_type") != "derived_series":
        return None
    match = re.fullmatch(
        rf"(?:what|how) about (?P<metric>{_METRIC})(?: (?P<aggregation>per game|total|in total))?(?: instead)?",
        _text(question),
    )
    if match is None:
        return None
    saved = context.get("series_question")
    players = context.get("players")
    if (
        not isinstance(saved, dict)
        or not isinstance(saved.get("query"), dict)
        or not isinstance(players, list)
        or len(players) != 1
    ):
        raise SemanticError(
            "clarification_required",
            "Restate the player, metric and series window; the saved series scope is incomplete.",
        )
    mode, n = saved.get("mode"), saved.get("rolling_n")
    if mode not in {"rolling", "cumulative", "trend"}:
        raise SemanticError("invalid_context", "The saved series operation is invalid.")
    args = dict(saved["query"])
    if args.get("player_id") != players[0].get("player_id"):
        raise SemanticError(
            "invalid_context", "The saved series identity is inconsistent."
        )
    metric = load_contract().metric(METRICS[match["metric"]])
    if mode == "cumulative" and (
        metric.denominator or match["aggregation"] == "per game"
    ):
        raise SemanticError(
            "clarification_required",
            "Cumulative series support count totals. Choose a counting statistic or restate a rolling average or dated trend for a percentage.",
        )
    if mode != "cumulative" and match["aggregation"] in {"total", "in total"}:
        raise SemanticError(
            "clarification_required",
            "The prior series shows per-appearance values or rolling averages. Restate a cumulative count series for totals.",
        )
    if metric.denominator and match["aggregation"] is not None:
        raise SemanticError(
            "clarification_required",
            "Shooting percentages use pooled components. Restate the percentage without a per-game or total aggregation.",
        )
    args.pop("player_id")
    args.update(
        player_name=players[0]["player_name"],
        metric=metric.key,
        aggregation="ratio"
        if metric.denominator
        else "total"
        if mode == "cumulative"
        else "average",
    )
    return DerivedSeriesPlan(args, mode, n)


def derived_series_question(plan: DerivedSeriesPlan) -> str:
    """Describe a retained plan for the host's scope/identity preflight.

    The validated structured query remains execution authority; this text keeps
    the normal host's source loading, route labels and name checks in agreement.
    """
    from app.agent.game_filters import filter_question

    query = plan.query
    metric = next(name for name, key in METRICS.items() if key == query["metric"])
    phase = query.get("season_type", "Regular Season")
    phase = "regular season and playoffs" if phase == "Both" else phase.casefold()
    season = f"{query['season']} {phase}"
    window = query.get("window", "season_to_date")
    if window == "date_range":
        period = f"from {query['start_date']} through {query['as_of']} in the {season}"
    elif window in {"last_n_games", "prior_n_games"}:
        modifier = "last" if window == "last_n_games" else "prior"
        period = f"in the {modifier} {query['n']} games in the {season}"
    else:
        period = f"in the {season}"
    if query.get("as_of") and window != "date_range":
        period += f" as of {query['as_of']}"
    measure = (
        f"rolling {plan.rolling_n}-game {metric} average"
        if plan.mode == "rolling"
        else f"cumulative {metric}"
        if plan.mode == "cumulative"
        else f"{metric} trend"
    )
    return (
        f"{query['player_name']}'s {measure} {period} {filter_question(query)}".rstrip()
    )


def derived_series_plan(
    question: str, selected_season: str, player_names: Sequence[str]
) -> DerivedSeriesPlan | None:
    text = re.sub(r"^(?:show|plot|chart) (?:me )?", "", _text(question))
    # Source identity bounds keep an unknown modifier out of the player slot.
    for name in sorted(set(player_names), key=len, reverse=True):
        prefix = re.escape(name.casefold()) + r"(?:'s)? "
        patterns = (
            (
                "rolling",
                rf"{prefix}rolling (?P<n>\d+)[ -](?:game|appearance) (?P<metric>{_METRIC})(?: (?:average|averages))? (?P<period>.+)",
            ),
            (
                "rolling",
                rf"{prefix}(?P<metric>{_METRIC}) rolling (?P<n>\d+)[ -](?:game|appearance) (?:average|averages) (?P<period>.+)",
            ),
            (
                "cumulative",
                rf"{prefix}cumulative (?P<metric>{_METRIC})(?: totals?)? (?P<period>.+)",
            ),
            (
                "cumulative",
                rf"{prefix}(?P<metric>{_METRIC}) cumulative totals? (?P<period>.+)",
            ),
            (
                "trend",
                rf"{prefix}(?P<metric>{_METRIC}) (?:trend|over time|by (?:game|appearance)) (?P<period>.+)",
            ),
        )
        for mode, pattern in patterns:
            match = re.fullmatch(pattern, text)
            if not match:
                continue
            period = explicit_period(match["period"], selected_season)
            if period is None:
                return None
            metric = load_contract().metric(METRICS[match["metric"]])
            if mode == "cumulative" and metric.denominator:
                return None  # Cumulative percentages are not count totals.
            n = int(match["n"]) if mode == "rolling" else None
            if n is not None and not 1 <= n <= 100:
                return None
            return DerivedSeriesPlan(
                {
                    "metric": metric.key,
                    "aggregation": "ratio"
                    if metric.denominator
                    else "total"
                    if mode == "cumulative"
                    else "average",
                    "player_name": name,
                    **period,
                },
                mode,
                n,
            )
    return None


def period_comparison_plan(
    question: str, selected_season: str, player_names: Sequence[str]
) -> dict[str, Any] | None:
    """Same player/stat, two complete explicit operands, standard executor."""
    text = re.sub(r"^compare ", "", _text(question))
    for name in sorted(set(player_names), key=len, reverse=True):
        match = re.fullmatch(
            rf"{re.escape(name.casefold())}(?:'s)? (?P<metric>{_METRIC})(?: (?P<mode>per game|total|in total))? (?P<left>.+?) (?:versus|vs|compared (?:with|to)) (?:the )?(?P<right>.+)",
            text,
        )
        if not match:
            continue
        left = explicit_period(match["left"], selected_season)
        right = explicit_period(match["right"], selected_season)
        if left is None or right is None:
            return None
        metric = load_contract().metric(METRICS[match["metric"]])
        if metric.denominator and match["mode"] is not None:
            return None
        if not metric.denominator and match["mode"] is None:
            return None
        aggregation = (
            "ratio"
            if metric.denominator
            else "average"
            if match["mode"] == "per game"
            else "total"
        )
        common = {
            "metric": metric.key,
            "aggregation": aggregation,
            "player_name": name,
            "operation": "summary",
            "window": "season_to_date",
        }
        return {
            "status": "compare",
            "model_calls": 0,
            "message": "",
            "queries": [{**common, **left}, {**common, **right}],
        }
    return None


def compute_derived_series(
    evidence: Evidence, query: Query, mode: str, rolling_n: int | None = None
) -> dict[str, Any]:
    if mode not in {"rolling", "cumulative", "trend"}:
        raise SemanticError("unsupported_operation", "Unsupported series operation")
    if query.player_id is None or query.operation != "summary":
        raise SemanticError("invalid_scope", "A series requires one resolved player")
    metric = load_contract().metric(query.metric)
    if mode == "rolling" and (type(rolling_n) is not int or not 1 <= rolling_n <= 100):
        raise SemanticError(
            "invalid_scope", "Rolling windows require 1 to 100 appearances"
        )
    if mode != "rolling" and rolling_n is not None:
        raise SemanticError(
            "invalid_scope", "Only rolling series take a rolling window"
        )
    required = (
        "ratio"
        if metric.denominator
        else "total"
        if mode == "cumulative"
        else "average"
    )
    if query.aggregation != required or (mode == "cumulative" and metric.denominator):
        raise SemanticError("unsupported_operation", "Unsupported series aggregation")
    selection = select_query_rows(evidence, query)
    rows = sorted(
        (r for r in selection.rows if r["player_id"] == query.player_id),
        key=lambda r: (str(r["game_date"]), str(r["game_id"])),
    )
    output = []
    for index, row in enumerate(rows):
        warmup = False
        if mode == "rolling":
            assert rolling_n is not None
            window = rows[max(0, index + 1 - rolling_n) : index + 1]
            warmup = len(window) < rolling_n
        else:
            window = rows[: index + 1] if mode == "cumulative" else [row]
        result = aggregate(window, metric, query.aggregation)
        if warmup:
            result["value"] = result["display_value"] = None
        output.append(
            {
                **{
                    k: row.get(k)
                    for k in (
                        "game_id",
                        "game_date",
                        "team_abbr",
                        "opponent_abbr",
                        "season_type",
                    )
                },
                **result,
                "warmup": warmup,
                "status": "warmup"
                if warmup
                else "unavailable"
                if result["value"] is None
                else "partial"
                if result["missing_component_games"]
                else "ok",
            }
        )
    through = {
        (
            phase if len(selection.seasons) == 1 else f"{season} {phase}"
        ): evidence.data_through[(season, phase)]
        for season, phase in selection.scopes
    }
    warnings = []
    if len(output) > query.limit:
        warnings.append("display_limit_applied_latest_games")
    if any(str(selection.anchor) > str(value) for value in through.values()):
        warnings.append("requested_as_of_exceeds_source_coverage")
    if query.n and query.window.endswith("games") and len(rows) < query.n:
        warnings.append("partial_window")
    if any(r["missing_component_games"] for r in output):
        warnings.append("incomplete_components")
    status = (
        "no_observations"
        if not rows
        else "unavailable"
        if mode == "trend" and len(rows) < 2
        else "ok"
    )
    return {
        "status": status,
        "schema_version": "derived_series/1",
        "contract_version": load_contract().version,
        "metric": metric.public(),
        "mode": mode,
        "rolling_n": rolling_n,
        "scope": {
            **asdict(query),
            "as_of": selection.anchor.isoformat(),
            "window_start": selection.start.isoformat() if selection.start else None,
            "window_end": selection.end.isoformat(),
        },
        "rows": output[-query.limit :],
        "observed_games": len(rows),
        "displayed_games": min(query.limit, len(output)),
        "warmup_games": min(len(rows), rolling_n - 1) if rolling_n else 0,
        "warnings": warnings,
        "provenance": {
            "source": evidence.source,
            "snapshot_id": evidence.snapshot_id,
            "data_through": through,
            "model_built_at": evidence.model_built_at,
            "temporal_semantics": "retrospective_source_snapshot",
        },
    }


def render_derived_series_answer(
    plan: DerivedSeriesPlan,
    evidence: Evidence,
    players: Sequence[Mapping[str, Any]],
    query_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from app.agent.game_filters import filter_description
    from app.agent.performance_overview import identity_profile

    args = dict(plan.query)
    name = args.pop("player_name")
    identity = resolve_entity(name, players)
    if identity["status"] != "ok":
        return answer_payload(
            "Please choose the intended player.",
            status="clarification_required",
            identity=identity,
        )
    args.update(query_fields or {})
    player = identity["matches"][0]
    args.update(player_id=player["player_id"], limit=100)
    query = Query(**args)
    result = compute_derived_series(evidence, query, plan.mode, plan.rolling_n)
    metric, scope = result["metric"], result["scope"]
    label = {
        "rolling": f"Rolling {plan.rolling_n}-appearance average",
        "cumulative": "Cumulative known-component total",
        "trend": "Per-appearance trend",
    }[plan.mode]
    title = f"{name}: {metric['label']} — {label}"
    payload = answer_payload(
        f"{title}. Showing {result['displayed_games']} of {result['observed_games']} selected appearances, oldest to newest.",
        status=result["status"],
        semantic_evidence=result,
        query_plan={
            "route": "governed_metrics",
            "series": plan.mode,
            "queries": [asdict(query)],
        },
        player_profile=identity_profile(player, evidence.rows),
        conversation_context={
            "analysis_type": "derived_series",
            "series_question": {
                "mode": plan.mode,
                "rolling_n": plan.rolling_n,
                "query": {**asdict(query), "as_of": scope["as_of"]},
            },
            "players": [
                {"player_id": player["player_id"], "player_name": player["player_name"]}
            ],
            "scope": scope,
            "metrics": [metric["key"]],
        },
    )
    if result["status"] == "unavailable":
        payload["answer"] = (
            "At least two selected appearances are required to show a trend."
        )
        return payload
    payload["assumptions"] = [
        f"{scope['season']} {scope['season_type']}; {scope['window']}; through {scope['as_of']}.",
        "Recorded appearances only, ordered by date and game ID. Missing appearances and components are not zero.",
        "All selected observations are used before the latest 100 display rows are retained.",
        f"Source: {evidence.source}; snapshot {evidence.snapshot_id}; data through {result['provenance']['data_through']}.",
        *result["warnings"],
    ]
    if plan.mode == "rolling":
        payload["assumptions"].append(
            f"Windows contain {plan.rolling_n} selected appearances, including appearances with missing components. The first {result['warmup_games']} are warmup and have no rolling value. Averages use valid appearances; ratios pool valid components, never average game percentages."
        )
    elif plan.mode == "cumulative":
        payload["assumptions"].append(
            "Totals start at the beginning of the selected scope and sum valid components only. Partial totals disclose omitted unknown components; they do not establish a complete total."
        )
    else:
        payload["assumptions"].append(
            "This shows dated observations, without inferring statistical significance or a fitted trend."
        )
    payload["assumptions"].append(
        "Zero denominators are unavailable. Partial values show their valid/observed sample; the chart is withheld when displayed values are partial or missing beyond warmup."
    )
    if note := filter_description(scope):
        payload["assumptions"].append(note)
    table_rows = []
    for row in result["rows"]:
        value = (
            "Warmup"
            if row["warmup"]
            else "Unavailable"
            if row["display_value"] is None
            else f"{row['display_value']:.1f}"
            + ("%" if metric["unit"] == "ratio" else "")
        )
        table_rows.append(
            [
                str(row["game_date"]),
                str(row["game_id"]),
                value,
                str(row["valid_games"]),
                str(row["observed_games"]),
                str(row["missing_component_games"]),
            ]
        )
    if table_rows:
        payload["tables"].append(
            {
                "title": title,
                "columns": [
                    {"key": key, "label": heading}
                    for key, heading in (
                        ("date", "Date"),
                        ("game_id", "Game ID"),
                        ("value", metric["label"]),
                        ("valid", "Games with data"),
                        ("observed", "Games in window"),
                        ("missing", "Games missing components"),
                    )
                ],
                "rows": table_rows,
            }
        )
    points = [r for r in result["rows"] if not r["warmup"]]
    if len(points) >= 2 and all(r["status"] == "ok" for r in points):
        payload["charts"].append(
            {
                "type": "line",
                "title": title,
                "x_label": "Appearance date",
                "y_label": label + (" (%)" if metric["unit"] == "ratio" else ""),
                "series": [
                    {
                        "key": metric["key"],
                        "label": name,
                        "points": [
                            {
                                "x": str(r["game_date"]),
                                "y": r["display_value"],
                                "meta": str(r["game_id"]),
                            }
                            for r in points
                        ],
                    }
                ],
            }
        )
    payload["metric_definitions"] = [
        {
            "key": metric["key"],
            "label": metric["label"],
            "definition": metric["numerator"]
            + (f" / ({metric['denominator']})" if metric["denominator"] else ""),
            "unit": metric["unit"],
        }
    ]
    return payload
