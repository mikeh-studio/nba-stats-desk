"""Offline core-stat models over typed, already-authorized accepted inputs.

Uses the existing governed arithmetic. This module cannot authorize source data.
"""

from __future__ import annotations

import copy
import math
import re
from collections import defaultdict
from datetime import datetime

from scripts.validate_publication_contract import (
    PROFILE,
    digest,
    metric_schema_validation_cache,
    validate_metric_records,
)

from app.agent.formulas import formula_metadata_cache
from app.agent.semantics import aggregate, load_contract

COUNTS = (
    "pts fgm fga fg3m fg3a ftm fta oreb dreb reb ast stl blk tov pf plus_minus".split()
)
CONTRACT = load_contract()


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def metrics(
    rows, model="core_player_summary", *, minute_precision="whole_minute_reported"
):
    result = []
    for key, spec in PROFILE["metrics"].items():
        for aggregation in spec["aggregations"]:
            if model == "core_performance" and aggregation == "per_game":
                continue
            governed = (
                "average"
                if aggregation == "per_game"
                else "ratio"
                if aggregation in {"ratio", "per36"}
                else "total"
            )
            value = aggregate(rows, CONTRACT.metrics[key], governed)
            unit = spec["unit"] + ("_per_game" if aggregation == "per_game" else "")
            missing = value["missing_component_games"]
            unavailable = (
                "no_appearances"
                if not rows
                else "missing_components"
                if not value["valid_games"]
                else "zero_denominator"
                if value["value"] is None
                else None
            )
            if key == "gp":
                value.update(value=len(rows), numerator=len(rows), denominator=None)
                unavailable = None
            result.append(
                dict(
                    metric=key,
                    aggregation=aggregation,
                    value=value["value"],
                    unit=unit,
                    observed_games=len(rows),
                    valid_games=value["valid_games"],
                    missing_games=missing,
                    numerator=value["numerator"],
                    denominator=value["denominator"],
                    precision=minute_precision
                    if key == "min"
                    else "minute_derived"
                    if aggregation == "per36"
                    else "ratio_derived"
                    if aggregation == "ratio"
                    else "exact_count",
                    unavailable_reason=unavailable,
                )
            )
    validate_metric_records(model, result, len(rows), minute_precision=minute_precision)
    return result


@formula_metadata_cache(True)
@metric_schema_validation_cache()
def build_models(accepted, *, materialized_at, release_scope=None):
    """Build all 13 relations; reject incomplete scope or malformed row joins."""
    require(
        accepted["status"] == "accepted", "held export cannot build publication models"
    )
    from scripts.weekly_profile import weekly_scope

    scope = PROFILE["scope"] if release_scope is None else weekly_scope(release_scope)
    minute_precision = (
        "second_reported" if scope["provider"] == "nba_api" else "whole_minute_reported"
    )
    require(accepted["scope"] == scope, "unsupported scope")
    require(
        datetime.fromisoformat(materialized_at.replace("Z", "+00:00")).tzinfo
        is not None,
        "materialization clock requires timezone",
    )
    games = copy.deepcopy(accepted["games"])
    appearances = copy.deepcopy(accepted["appearances"])
    participants = copy.deepcopy(accepted["participants"])
    players = copy.deepcopy(accepted["players"])
    teams = copy.deepcopy(accepted["teams"])
    require(0 < len(games) <= 1500 and len(appearances) <= 100000, "input bound")
    game_by_id = {g["game_id"]: g for g in games}
    player_by_id = {p["player_id"]: p for p in players}
    team_by_id = {t["team_id"]: t for t in teams}
    require(
        len(game_by_id) == len(games)
        and len({g["game_uid"] for g in games}) == len(games),
        "duplicate game",
    )
    require(
        len(player_by_id) == len(players) and len(team_by_id) == len(teams),
        "duplicate dimension",
    )
    require(
        len({t["team_abbr"] for t in teams}) == len(teams),
        "duplicate team abbreviation",
    )
    for g in games:
        require(
            re.fullmatch(r"\d{10}", g["game_id"])
            and g["season"] == scope["season"]
            and g["season_type"] == scope["phase"]
            and scope["first"] <= g["game_date"] <= scope["last"],
            "game scope/identity differs",
        )
        require(
            g["home_team_id"] != g["away_team_id"]
            and g["home_score"] != g["away_score"],
            "invalid final teams/result",
        )
        for side in ("home", "away"):
            require(
                type(g[side + "_score"]) is int and g[side + "_score"] >= 0,
                "invalid final score",
            )
            require(
                team_by_id.get(g[side + "_team_id"], {}).get("team_abbr")
                == g[side + "_team_abbr"],
                "unmapped team",
            )
    participant_by_key = {}
    for p in participants:
        key = (p["game_id"], p["player_id"])
        require(
            key not in participant_by_key
            and type(p["player_id"]) is int
            and p["player_id"] > 0
            and type(p["played"]) is bool
            and p["game_id"] in game_by_id,
            "invalid/duplicate participant",
        )
        g = game_by_id[p["game_id"]]
        require(
            p["team_abbr"] in {g["home_team_abbr"], g["away_team_abbr"]},
            "participant team differs",
        )
        participant_by_key[key] = p
    facts, seen = [], set()
    for a in appearances:
        key = (a["game_id"], a["player_id"])
        require(
            key not in seen
            and key in participant_by_key
            and participant_by_key[key]["played"] is True
            and participant_by_key[key]["team_abbr"] == a["team_abbr"],
            "appearance/participant mismatch",
        )
        seen.add(key)
        g, p = game_by_id[a["game_id"]], player_by_id.get(a["player_id"])
        require(
            p is not None
            and isinstance(p["player_name"], str)
            and p["player_name"].strip(),
            "missing player reference",
        )
        require(type(a["player_id"]) is int and a["player_id"] > 0, "invalid player ID")
        side = "home" if a["team_abbr"] == g["home_team_abbr"] else "away"
        other = "away" if side == "home" else "home"
        require(
            a["opponent_abbr"] == g[other + "_team_abbr"]
            and a["home_away"] == side.upper(),
            "opponent/side mismatch",
        )
        require(
            all(type(a[k]) is int and (a[k] >= 0 or k == "plus_minus") for k in COUNTS),
            "missing/nonintegral count",
        )
        require(
            a["pts"] == 2 * a["fgm"] + a["fg3m"] + a["ftm"]
            and a["reb"] == a["oreb"] + a["dreb"],
            "count reconciliation",
        )
        require(
            all(
                a[m] <= a[t]
                for m, t in [("fgm", "fga"), ("fg3m", "fg3a"), ("ftm", "fta")]
            )
            and a["fg3m"] <= a["fgm"]
            and a["fg3a"] <= a["fga"]
            and a["fgm"] - a["fg3m"] <= a["fga"] - a["fg3a"],
            "invalid shot counts",
        )
        require(
            a["min"] is None
            or (
                type(a["min"]) in (int, float)
                and 0 <= a["min"] <= 75
                and math.isfinite(a["min"])
                and (
                    int(a["min"]) == a["min"]
                    if minute_precision == "whole_minute_reported"
                    else math.isclose(
                        a["min"] * 60, round(a["min"] * 60), rel_tol=0, abs_tol=1e-7
                    )
                )
                and a["minutes_precision"] == minute_precision
                and a["minutes_zero_origin"] != "unverified_legacy_zero"
            ),
            "invalid minute precision",
        )
        row = {
            k: a[k]
            for k in COUNTS
            + ["game_id", "player_id", "team_abbr", "opponent_abbr", "home_away", "min"]
        }
        row.update(
            player_name=p["player_name"],
            game_date=g["game_date"],
            season=g["season"],
            season_type=g["season_type"],
            matchup=f"{a['team_abbr']} {'vs.' if side == 'home' else '@'} {a['opponent_abbr']}",
            wl="W" if g[side + "_score"] > g[other + "_score"] else "L",
            fg_pct=a["fgm"] / a["fga"] if a["fga"] else None,
            ft_pct=a["ftm"] / a["fta"] if a["fta"] else None,
            fantasy_points_simple=round(
                a["pts"] + a["reb"] + a["ast"] + a["stl"] + a["blk"] - a["tov"], 1
            ),
            ingested_at_utc=materialized_at,
        )
        facts.append(row)
    require(
        seen == {k for k, p in participant_by_key.items() if p["played"]},
        "missing played participant",
    )
    require(
        set(player_by_id) == {r["player_id"] for r in facts},
        "player dimension coverage differs",
    )
    team_results = []
    for g in games:
        for side, other in [("home", "away"), ("away", "home")]:
            team_rows = [
                r
                for r in facts
                if r["game_id"] == g["game_id"]
                and r["team_abbr"] == g[side + "_team_abbr"]
            ]
            require(
                team_rows and sum(r["pts"] for r in team_rows) == g[side + "_score"],
                "player/final score mismatch",
            )
            team_results.append(
                dict(
                    game_id=g["game_id"],
                    season=g["season"],
                    season_type=g["season_type"],
                    game_date=g["game_date"],
                    team_id=g[side + "_team_id"],
                    team_abbr=g[side + "_team_abbr"],
                    opponent_team_id=g[other + "_team_id"],
                    opponent_team_abbr=g[other + "_team_abbr"],
                    home_away=side.upper(),
                    team_pts=g[side + "_score"],
                    opponent_team_pts=g[other + "_score"],
                    scoring_margin=g[side + "_score"] - g[other + "_score"],
                    game_result="W"
                    if g[side + "_score"] > g[other + "_score"]
                    else "L",
                )
            )
    require(
        set(team_by_id) == {r["team_id"] for r in team_results},
        "team dimension coverage differs",
    )
    by_player = defaultdict(list)
    for r in sorted(
        facts,
        key=lambda r: (r["game_date"], r["game_id"], r["player_id"]),
        reverse=True,
    ):
        by_player[r["player_id"]].append(r)
    summaries, windows, trends, search = [], [], [], []
    for pid, rows in sorted(by_player.items()):
        summary_metrics = metrics(rows, minute_precision=minute_precision)
        summaries.append(
            dict(
                player_id=pid,
                player_name=player_by_id[pid]["player_name"],
                observed_games=len(rows),
                first_game_date=rows[-1]["game_date"],
                last_game_date=rows[0]["game_date"],
                metrics=summary_metrics,
            )
        )
        search.append(
            dict(
                player_id=pid,
                player_name=player_by_id[pid]["player_name"],
                team_abbrs=sorted({r["team_abbr"] for r in rows}),
                observed_games=len(rows),
            )
        )
        slices = {
            "scope": rows,
            "last_5": rows[:5],
            "prior_5": rows[5:10],
            "last_10": rows[:10],
        }
        window_metrics = {}
        for key, part in slices.items():
            if key == "scope":
                values = copy.deepcopy(summary_metrics)
                validate_metric_records(
                    "core_player_windows",
                    values,
                    len(part),
                    minute_precision=minute_precision,
                )
            else:
                values = metrics(
                    part, "core_player_windows", minute_precision=minute_precision
                )
            window_metrics[key] = values
            windows.append(
                dict(
                    player_id=pid,
                    window_key=key,
                    first_game_date=part[-1]["game_date"] if part else None,
                    last_game_date=part[0]["game_date"] if part else None,
                    observed_games=len(part),
                    metrics=values,
                )
            )
        # Trend inputs share the same arithmetic as these windows. Preserve
        # the separate summary-model validation before deriving trend values.
        for key in ("last_5", "prior_5"):
            validate_metric_records(
                "core_player_summary",
                window_metrics[key],
                len(slices[key]),
                minute_precision=minute_precision,
            )
        recent = {(r["metric"], r["aggregation"]): r for r in window_metrics["last_5"]}
        prior = {(r["metric"], r["aggregation"]): r for r in window_metrics["prior_5"]}
        for metric, agg in PROFILE["trend_metrics"].items():
            a, b = recent[metric, agg], prior[metric, agg]
            ok = (
                a["valid_games"] == b["valid_games"] == 5
                and a["value"] is not None
                and b["value"] is not None
            )
            trends.append(
                dict(
                    player_id=pid,
                    metric=metric,
                    aggregation=agg,
                    recent_valid_games=a["valid_games"],
                    prior_valid_games=b["valid_games"],
                    recent_value=a["value"],
                    prior_value=b["value"],
                    change=(a["value"] - b["value"]) * (100 if agg == "ratio" else 1)
                    if ok
                    else None,
                    change_unit="percentage_points" if agg == "ratio" else a["unit"],
                    unavailable_reason=None
                    if ok
                    else "requires_two_complete_five_game_windows",
                )
            )
    models = dict(
        accepted_game_results=games,
        accepted_player_appearances=appearances,
        participant_manifest=participants,
        dim_player=players,
        dim_team=teams,
        fct_player_game_stats=facts,
        fct_team_game_results=team_results,
        dim_game=[
            {k: g[k] for k in PROFILE["assets"]["dim_game"]["required_fields"]}
            for g in games
        ],
        core_player_summary=summaries,
        core_player_windows=windows,
        core_player_trends=trends,
        core_player_search=search,
        core_performance=[
            {
                **{
                    k: r[k]
                    for k in [
                        "game_id",
                        "game_date",
                        "player_id",
                        "player_name",
                        "team_abbr",
                        "opponent_abbr",
                    ]
                },
                "metrics": metrics(
                    [r], "core_performance", minute_precision=minute_precision
                ),
            }
            for r in facts
        ],
    )
    for name, rows in models.items():
        expected = set(PROFILE["assets"][name]["required_fields"])
        require(all(set(r) == expected for r in rows), f"{name} field mismatch")
        rows.sort(key=digest)
    return models
