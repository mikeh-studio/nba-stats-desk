"""Compile isolated BigQuery candidate SELECTs, without a client or execution.

Inputs are five typed, immutable staging tables from the accepted export. This
compiler never reads the legacy raw tables or resolves a mutable current view.
Phase 4 must prove BigQuery execution and enforce the input/output digests.
"""

from __future__ import annotations

import re
from datetime import datetime

from app.serving.core_models import CONTRACT, PROFILE


def metric_array(model, *, minute_precision="whole_minute_reported"):
    if minute_precision not in {"whole_minute_reported", "second_reported"}:
        raise ValueError("unsupported minute precision")
    slots = []
    observed = "COUNT(f.game_id)"
    for key, spec in PROFILE["metrics"].items():
        metric = CONTRACT.metrics[key]
        valid = "f.game_id IS NOT NULL" + "".join(
            f" AND f.{c} IS NOT NULL" for c in sorted(metric.components)
        )
        count = f"COUNTIF({valid})"

        def formula(expression):
            return re.sub(
                r"\b[a-z][a-z0-9_]*\b",
                lambda m: f"SUM(IF({valid}, f.{m[0]}, NULL))",
                expression,
            )

        numerator = observed if key == "gp" else formula(metric.numerator)
        for agg in spec["aggregations"]:
            if model == "core_performance" and agg == "per_game":
                continue
            denominator = (
                formula(metric.denominator)
                if metric.denominator
                else f"NULLIF({count}, 0)"
                if agg == "per_game"
                else "CAST(NULL AS FLOAT64)"
            )
            value = (
                f"SAFE_DIVIDE({numerator}, {denominator})"
                if agg in {"per_game", "ratio", "per36"}
                else numerator
            )
            unit = spec["unit"] + ("_per_game" if agg == "per_game" else "")
            precision = (
                minute_precision
                if key == "min"
                else "minute_derived"
                if agg == "per36"
                else "ratio_derived"
                if agg == "ratio"
                else "exact_count"
            )
            reason = (
                "CAST(NULL AS STRING)"
                if key == "gp"
                else f"CASE WHEN {observed}=0 THEN 'no_appearances' WHEN {count}=0 THEN 'missing_components' WHEN ({value}) IS NULL THEN 'zero_denominator' END"
            )
            slots.append(
                f"STRUCT('{key}' AS metric, '{agg}' AS aggregation, CAST({value} AS FLOAT64) AS value, '{unit}' AS unit, {observed} AS observed_games, {count} AS valid_games, {observed}-{count} AS missing_games, CAST({numerator} AS FLOAT64) AS numerator, CAST({denominator} AS FLOAT64) AS denominator, '{precision}' AS precision, {reason} AS unavailable_reason)"
            )
    return "[\n" + ",\n".join(slots) + "\n]"


def compile_candidate(
    *, project, staging_dataset, candidate_dataset, materialized_at, provider="espn"
):
    """Return all 13 SELECT statements in dependency order; no implicit defaults."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project):
        raise ValueError("invalid project")
    if (
        any(
            not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,100}", d)
            for d in [staging_dataset, candidate_dataset]
        )
        or staging_dataset == candidate_dataset
    ):
        raise ValueError("distinct immutable staging/candidate datasets required")
    if provider not in {"espn", "nba_api"}:
        raise ValueError("unsupported source provider")
    minute_precision = (
        "second_reported" if provider == "nba_api" else "whole_minute_reported"
    )
    clock = datetime.fromisoformat(materialized_at.replace("Z", "+00:00"))
    if clock.tzinfo is None:
        raise ValueError("materialization timezone required")

    def table(name, staging=False):
        return f"`{project}.{staging_dataset if staging else candidate_dataset}.{name}`"

    def fields(asset, alias=""):
        return ", ".join(alias + k for k in PROFILE["assets"][asset]["required_fields"])

    sql = {}
    for name, source in [
        ("accepted_game_results", "games"),
        ("accepted_player_appearances", "appearances"),
        ("participant_manifest", "participants"),
        ("dim_player", "players"),
        ("dim_team", "teams"),
    ]:
        sql[name] = f"SELECT {fields(name)} FROM {table(source, True)}"
    sql["dim_game"] = (
        f"SELECT {fields('dim_game')} FROM {table('accepted_game_results')}"
    )
    derived = {
        "player_name": "p.player_name",
        "game_date": "g.game_date",
        "season": "g.season",
        "season_type": "g.season_type",
        "matchup": "CONCAT(a.team_abbr, IF(a.home_away='HOME',' vs. ',' @ '), a.opponent_abbr)",
        "wl": "IF((a.home_away='HOME') = (g.home_score>g.away_score),'W','L')",
        "fg_pct": "SAFE_DIVIDE(a.fgm,a.fga)",
        "ft_pct": "SAFE_DIVIDE(a.ftm,a.fta)",
        "fantasy_points_simple": "ROUND(a.pts+a.reb+a.ast+a.stl+a.blk-a.tov,1)",
        "ingested_at_utc": f"TIMESTAMP('{clock.isoformat()}')",
    }
    sql["fct_player_game_stats"] = (
        "SELECT "
        + ", ".join(
            f"{derived.get(k, 'a.' + k)} AS {k}"
            for k in PROFILE["assets"]["fct_player_game_stats"]["required_fields"]
        )
        + f" FROM {table('accepted_player_appearances')} a JOIN {table('accepted_game_results')} g USING(game_id) JOIN {table('dim_player')} p USING(player_id)"
    )
    parts = []
    for side, other in [("home", "away"), ("away", "home")]:
        parts.append(
            f"SELECT game_id, season, season_type, game_date, {side}_team_id AS team_id, {side}_team_abbr AS team_abbr, {other}_team_id AS opponent_team_id, {other}_team_abbr AS opponent_team_abbr, '{side.upper()}' AS home_away, {side}_score AS team_pts, {other}_score AS opponent_team_pts, {side}_score-{other}_score AS scoring_margin, IF({side}_score>{other}_score,'W','L') AS game_result FROM {table('accepted_game_results')}"
        )
    sql["fct_team_game_results"] = " UNION ALL ".join(parts)
    fact = table("fct_player_game_stats")
    sql["core_player_summary"] = (
        f"SELECT f.player_id, f.player_name, COUNT(*) AS observed_games, MIN(f.game_date) AS first_game_date, MAX(f.game_date) AS last_game_date, {metric_array('core_player_summary', minute_precision=minute_precision)} AS metrics FROM {fact} f GROUP BY f.player_id, f.player_name"
    )
    sql[
        "core_player_windows"
    ] = f"""WITH ranked AS (SELECT *, ROW_NUMBER() OVER(PARTITION BY player_id ORDER BY game_date DESC, game_id DESC) AS rn FROM {fact}), players AS (SELECT DISTINCT player_id FROM ranked)
SELECT p.player_id, window_key, MIN(f.game_date) AS first_game_date, MAX(f.game_date) AS last_game_date, COUNT(f.game_id) AS observed_games, {metric_array("core_player_windows", minute_precision=minute_precision)} AS metrics
FROM players p CROSS JOIN UNNEST(['scope','last_5','prior_5','last_10']) window_key
LEFT JOIN ranked f ON p.player_id=f.player_id AND (window_key='scope' OR (window_key='last_5' AND rn<=5) OR (window_key='prior_5' AND rn BETWEEN 6 AND 10) OR (window_key='last_10' AND rn<=10)) GROUP BY p.player_id, window_key"""
    trend_filter = " OR ".join(
        f"(a.metric='{k}' AND a.aggregation='{v}')"
        for k, v in PROFILE["trend_metrics"].items()
    )
    good = "a.valid_games=5 AND b.valid_games=5 AND a.value IS NOT NULL AND b.value IS NOT NULL"
    sql["core_player_trends"] = (
        f"""SELECT recent.player_id, a.metric, a.aggregation, a.valid_games AS recent_valid_games, b.valid_games AS prior_valid_games, a.value AS recent_value, b.value AS prior_value, IF({good},(a.value-b.value)*IF(a.aggregation='ratio',100,1),NULL) AS change, IF(a.aggregation='ratio','percentage_points',a.unit) AS change_unit, IF({good},NULL,'requires_two_complete_five_game_windows') AS unavailable_reason FROM {table("core_player_windows")} recent JOIN {table("core_player_windows")} prior ON recent.player_id=prior.player_id CROSS JOIN UNNEST(recent.metrics) a CROSS JOIN UNNEST(prior.metrics) b WHERE recent.window_key='last_5' AND prior.window_key='prior_5' AND a.metric=b.metric AND a.aggregation=b.aggregation AND ({trend_filter})"""
    )
    cols = "f.game_id, f.game_date, f.player_id, f.player_name, f.team_abbr, f.opponent_abbr"
    sql["core_performance"] = (
        f"SELECT {cols}, {metric_array('core_performance', minute_precision=minute_precision)} AS metrics FROM {fact} f GROUP BY {cols}"
    )
    sql["core_player_search"] = (
        f"SELECT player_id, player_name, ARRAY_AGG(DISTINCT team_abbr ORDER BY team_abbr) AS team_abbrs, COUNT(*) AS observed_games FROM {fact} GROUP BY player_id, player_name"
    )
    assert set(sql) == set(PROFILE["assets"])
    return sql
