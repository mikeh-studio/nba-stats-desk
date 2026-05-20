from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import sqrt
from typing import Any, Literal, Protocol

from google.api_core.exceptions import GoogleAPIError as BQAPIError
from google.cloud import bigquery

from app.agent.catalog import load_semantic_catalog
from app.agent.formulas import FormulaError, compile_formula_sql
from app.config import SUPPORTED_SEASON, Settings

STATE_FRESH = "fresh"
STATE_STALE = "stale"
STATE_MISSING = "missing"
STATE_INSUFFICIENT_SAMPLE = "insufficient_sample"
STATE_UNAVAILABLE = "unavailable"

CompareWindow = Literal["last_3", "last_5", "last_7", "prior_5", "last_10"]
CompareFocus = Literal["balanced", "scoring", "playmaking", "defense"]

COMPARE_WINDOW_CONFIG: dict[CompareWindow, dict[str, Any]] = {
    "last_3": {"label": "Last 3", "expected_games": 3},
    "last_5": {"label": "Last 5", "expected_games": 5},
    "last_7": {"label": "Last 7", "expected_games": 7},
    "prior_5": {"label": "Prior 5", "expected_games": 5},
    "last_10": {"label": "Last 10", "expected_games": 10},
}

COMPARE_METRIC_LABELS: dict[str, str] = {
    "fantasy_proxy_score": "Box Score Index",
    "avg_min": "Minutes",
    "avg_pts": "PTS",
    "avg_reb": "REB",
    "avg_ast": "AST",
    "avg_stl": "STL",
    "avg_blk": "BLK",
    "avg_fg3m": "3PM",
    "avg_tov": "TOV",
}

COMPARE_FOCUS_CONFIG: dict[CompareFocus, dict[str, Any]] = {
    "balanced": {
        "label": "Balanced",
        "description": "Keeps the full stat mix in the default read order.",
        "metric_keys": [
            "fantasy_proxy_score",
            "avg_min",
            "avg_pts",
            "avg_reb",
            "avg_ast",
            "avg_stl",
            "avg_blk",
            "avg_fg3m",
            "avg_tov",
        ],
    },
    "scoring": {
        "label": "Scoring",
        "description": "Pushes scoring volume and shot-making to the top of the comparison.",
        "metric_keys": [
            "fantasy_proxy_score",
            "avg_pts",
            "avg_fg3m",
            "avg_min",
            "avg_ast",
            "avg_reb",
            "avg_stl",
            "avg_blk",
            "avg_tov",
        ],
    },
    "playmaking": {
        "label": "Playmaking",
        "description": "Prioritizes creation load first, then supporting scoring context.",
        "metric_keys": [
            "avg_ast",
            "avg_min",
            "avg_tov",
            "fantasy_proxy_score",
            "avg_pts",
            "avg_reb",
            "avg_stl",
            "avg_blk",
            "avg_fg3m",
        ],
    },
    "defense": {
        "label": "Defense",
        "description": "Highlights defensive events and rebounding before offense-first stats.",
        "metric_keys": [
            "avg_stl",
            "avg_blk",
            "avg_reb",
            "fantasy_proxy_score",
            "avg_min",
            "avg_pts",
            "avg_ast",
            "avg_fg3m",
            "avg_tov",
        ],
    },
}

SIMILARITY_RESULT_LIMIT = 6
SIMILARITY_FEATURE_COLUMNS = [
    "season_avg_pts",
    "season_avg_fga",
    "season_fg_pct",
    "season_ts_pct",
    "season_fg3a_rate",
    "season_fta_rate",
    "season_ast_to_tov",
    "team_points_contribution_rate",
    "team_fga_contribution_rate",
    "team_ast_contribution_rate",
    "team_tov_contribution_rate",
    "team_offense_contribution_rate",
    "team_reb_contribution_rate",
    "team_stl_contribution_rate",
    "team_blk_contribution_rate",
    "team_defense_contribution_rate",
    "shot_rim_rate",
    "shot_paint_non_ra_rate",
    "shot_midrange_rate",
    "shot_corner3_rate",
    "shot_above_break3_rate",
    "shot_rim_fg_pct",
    "shot_corner3_fg_pct",
    "season_avg_reb",
    "season_avg_ast",
    "season_avg_stl",
    "season_avg_blk",
    "season_avg_fg3m",
    "season_avg_tov",
    "season_avg_min",
    "height_inches",
    "weight_lbs",
    "season_exp",
    "recent_pts",
    "recent_reb",
    "recent_ast",
    "recent_stl",
    "recent_blk",
    "recent_fg3m",
    "recent_tov",
    "recent_min",
    "recent_points_share_of_team",
    "recent_points_share_of_game",
    "minutes_delta_vs_season",
    "second_half_pts_delta",
    "second_half_min_delta",
    "second_half_ts_delta",
]

SIMILARITY_FEATURE_WEIGHTS: dict[str, float] = {
    feature_name: 1.0 for feature_name in SIMILARITY_FEATURE_COLUMNS
}

SIMILARITY_TRAIT_LABELS: dict[str, str] = {
    "season_avg_pts": "scoring volume",
    "season_avg_fga": "shot volume",
    "season_fg_pct": "field-goal efficiency",
    "season_ts_pct": "true shooting",
    "season_fg3a_rate": "three-point diet",
    "season_fta_rate": "rim pressure",
    "season_ast_to_tov": "ball security",
    "team_points_contribution_rate": "team scoring share",
    "team_fga_contribution_rate": "team shot share",
    "team_ast_contribution_rate": "team assist share",
    "team_tov_contribution_rate": "team turnover load",
    "team_offense_contribution_rate": "team offense ownership",
    "team_reb_contribution_rate": "team rebounding share",
    "team_stl_contribution_rate": "team steal share",
    "team_blk_contribution_rate": "team block share",
    "team_defense_contribution_rate": "team defensive event share",
    "shot_rim_rate": "rim shot diet",
    "shot_paint_non_ra_rate": "paint shot diet",
    "shot_midrange_rate": "midrange diet",
    "shot_corner3_rate": "corner-three diet",
    "shot_above_break3_rate": "above-break three diet",
    "shot_rim_fg_pct": "rim finishing",
    "shot_corner3_fg_pct": "corner-three efficiency",
    "season_avg_reb": "rebounding",
    "season_avg_ast": "playmaking",
    "season_avg_stl": "steals pressure",
    "season_avg_blk": "rim protection",
    "season_avg_fg3m": "three-point volume",
    "season_avg_min": "minutes load",
    "height_inches": "height",
    "weight_lbs": "frame strength",
    "season_exp": "experience",
    "recent_points_share_of_team": "usage share",
    "recent_points_share_of_game": "game scoring share",
    "minutes_delta_vs_season": "minutes trend",
    "second_half_pts_delta": "second-half scoring growth",
    "second_half_min_delta": "second-half role growth",
    "second_half_ts_delta": "second-half efficiency growth",
}

STAT_PERCENTILE_CONFIG: tuple[dict[str, str], ...] = (
    {
        "key": "pts",
        "label": "PTS",
        "average_field": "season_avg_pts",
        "percentile_field": "pts_percentile",
        "baseline_field": "league_avg_pts",
        "direction": "higher",
    },
    {
        "key": "reb",
        "label": "REB",
        "average_field": "season_avg_reb",
        "percentile_field": "reb_percentile",
        "baseline_field": "league_avg_reb",
        "direction": "higher",
    },
    {
        "key": "ast",
        "label": "AST",
        "average_field": "season_avg_ast",
        "percentile_field": "ast_percentile",
        "baseline_field": "league_avg_ast",
        "direction": "higher",
    },
    {
        "key": "stl",
        "label": "STL",
        "average_field": "season_avg_stl",
        "percentile_field": "stl_percentile",
        "baseline_field": "league_avg_stl",
        "direction": "higher",
    },
    {
        "key": "blk",
        "label": "BLK",
        "average_field": "season_avg_blk",
        "percentile_field": "blk_percentile",
        "baseline_field": "league_avg_blk",
        "direction": "higher",
    },
    {
        "key": "tov",
        "label": "Ball Security",
        "average_field": "season_avg_tov",
        "percentile_field": "tov_percentile",
        "baseline_field": "league_avg_tov",
        "direction": "lower",
    },
)

TREND_STAT_ORDER = {
    "PTS": 1,
    "REB": 2,
    "AST": 3,
    "STL": 4,
    "BLK": 5,
    "TOV": 6,
    "MIN": 7,
    "FANTASY_POINTS_SIMPLE": 8,
}

AGENT_METRIC_LEADER_CONFIG: dict[str, dict[str, str]] = {
    "pts": {
        "label": "PTS",
        "column": "avg_pts",
        "percentile_column": "pts_percentile",
        "order": "DESC",
    },
    "reb": {
        "label": "REB",
        "column": "avg_reb",
        "percentile_column": "reb_percentile",
        "order": "DESC",
    },
    "ast": {
        "label": "AST",
        "column": "avg_ast",
        "percentile_column": "ast_percentile",
        "order": "DESC",
    },
    "stl": {
        "label": "STL",
        "column": "avg_stl",
        "percentile_column": "stl_percentile",
        "order": "DESC",
    },
    "blk": {
        "label": "BLK",
        "column": "avg_blk",
        "percentile_column": "blk_percentile",
        "order": "DESC",
    },
    "tov": {
        "label": "TOV",
        "column": "avg_tov",
        "percentile_column": "tov_percentile",
        "order": "ASC",
    },
    "fg3m": {
        "label": "3PM",
        "column": "avg_fg3m",
        "percentile_column": "NULL",
        "order": "DESC",
    },
    "min": {
        "label": "MIN",
        "column": "avg_min",
        "percentile_column": "NULL",
        "order": "DESC",
    },
    "fantasy_points_simple": {
        "label": "BSI",
        "column": "avg_fantasy_points_simple",
        "percentile_column": "NULL",
        "order": "DESC",
    },
}

AGENT_METRIC_COLUMN_MAP: dict[str, str] = {
    "pts": "avg_pts",
    "reb": "avg_reb",
    "ast": "avg_ast",
    "stl": "avg_stl",
    "blk": "avg_blk",
    "fg3m": "avg_fg3m",
    "tov": "avg_tov",
    "min": "avg_min",
    "fantasy_points_simple": "avg_fantasy_points_simple",
}


def _get_agent_metric_leader_config(metric: str) -> dict[str, str] | None:
    metric_def = load_semantic_catalog().metrics.get(metric)
    if metric_def is None:
        return AGENT_METRIC_LEADER_CONFIG.get(metric)
    if metric_def.formula:
        try:
            column = compile_formula_sql(metric_def.formula, AGENT_METRIC_COLUMN_MAP)
        except FormulaError:
            return None
    else:
        column = metric_def.leaderboard_column
    if not column:
        return None
    order = "ASC" if metric_def.direction == "lower" else "DESC"
    return {
        "label": metric_def.label,
        "column": column,
        "percentile_column": metric_def.percentile_key or "NULL",
        "order": order,
    }


def _to_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    return str(value)


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(float(value))


def _to_bool(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "t", "1", "yes"):
            return True
        if normalized in ("false", "f", "0", "no"):
            return False
    return bool(value)


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def _parse_iso_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _stringify_reason_value(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _reason_label(code: str | None) -> str | None:
    labels = {
        "recent_fp_delta": "recent box score production",
        "minutes_delta": "minutes trend",
        "games_next_7d": "next 7 days",
        "back_to_back_count": "back-to-backs",
        "trend_stat_delta": "recent trend",
        "category_edge": "category edge",
    }
    if code is None:
        return None
    return labels.get(code, code.replace("_", " "))


def get_compare_window_options() -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "label": str(config["label"]),
            "expected_games": int(config["expected_games"]),
        }
        for key, config in COMPARE_WINDOW_CONFIG.items()
    ]


def get_compare_focus_options() -> list[dict[str, str]]:
    return [
        {
            "key": key,
            "label": str(config["label"]),
            "description": str(config["description"]),
        }
        for key, config in COMPARE_FOCUS_CONFIG.items()
    ]


def _compare_window_label(window: CompareWindow) -> str:
    return str(COMPARE_WINDOW_CONFIG[window]["label"])


def _compare_window_expected_games(window: CompareWindow) -> int:
    return int(COMPARE_WINDOW_CONFIG[window]["expected_games"])


def _empty_compare_metrics() -> dict[str, Any]:
    return {key: None for key in COMPARE_METRIC_LABELS}


def _build_compare_metric_rows(
    metrics: dict[str, Any], focus: CompareFocus
) -> list[dict[str, Any]]:
    ordered_keys = list(COMPARE_FOCUS_CONFIG[focus]["metric_keys"])
    return [
        {
            "key": key,
            "label": COMPARE_METRIC_LABELS[key],
            "value": metrics.get(key),
            "is_focus": index < 3,
        }
        for index, key in enumerate(ordered_keys)
    ]


def build_reason_summary(row: dict[str, Any]) -> str | None:
    parts: list[str] = []
    for code_key, value_key in (
        ("reason_primary_code", "reason_primary_value"),
        ("reason_secondary_code", "reason_secondary_value"),
        ("reason_context_code", "reason_context_value"),
    ):
        label = _reason_label(row.get(code_key))
        value = _stringify_reason_value(row.get(value_key))
        if label and value:
            parts.append(f"{label}: {value}")
        elif label:
            parts.append(label)
    return " | ".join(parts) if parts else None


def build_headshot_url(player_id: Any) -> str | None:
    normalized_player_id = _to_int(player_id)
    if normalized_player_id is None:
        return None
    return (
        "https://cdn.nba.com/headshots/nba/latest/1040x760/"
        f"{normalized_player_id}.png"
    )


def build_player_initials(player_name: Any) -> str:
    if not isinstance(player_name, str) or not player_name.strip():
        return "NBA"
    parts = [part[0].upper() for part in player_name.split() if part]
    if not parts:
        return "NBA"
    return "".join(parts[:2])


def _format_home_date_label(value: str) -> str:
    parsed = _parse_iso_date(value)
    if parsed is None:
        return value
    return parsed.strftime("%a %b %d")


def _build_top_improvement_chips(row: dict[str, Any]) -> list[dict[str, Any]]:
    deltas: list[tuple[str, float]] = []
    for label, key in (
        ("PTS", "pts_delta"),
        ("REB", "reb_delta"),
        ("AST", "ast_delta"),
        ("STL", "stl_delta"),
        ("BLK", "blk_delta"),
        ("3PM", "fg3m_delta"),
        ("MIN", "min_delta"),
    ):
        value = _to_float(row.get(key))
        if value is None or value <= 0:
            continue
        deltas.append((label, round(value, 1)))
    deltas.sort(key=lambda item: item[1], reverse=True)
    return [{"label": label, "delta": value} for label, value in deltas[:3]]


def _sanitize_category_list(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    filtered = [item for item in items if item != "TOV"]
    if not filtered:
        return None
    return ", ".join(filtered)


def _split_display_list(value: Any) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _similarity_state_from_sample_status(sample_status: Any) -> str:
    if sample_status == "insufficient_sample":
        return STATE_INSUFFICIENT_SAMPLE
    if sample_status in ("ready", "limited_sample"):
        return STATE_FRESH
    return STATE_UNAVAILABLE


def _similarity_feature_value(row: dict[str, Any], feature_name: str) -> float | None:
    return _to_float(row.get(f"norm_{feature_name}"))


def _weighted_similarity_vector(row: dict[str, Any]) -> dict[str, float]:
    components: dict[str, float] = {}
    squared_norm = 0.0
    for feature_name in SIMILARITY_FEATURE_COLUMNS:
        value = _similarity_feature_value(row, feature_name) or 0.0
        component = value * SIMILARITY_FEATURE_WEIGHTS.get(feature_name, 1.0)
        components[feature_name] = component
        squared_norm += component**2

    if squared_norm <= 1e-12:
        return {feature_name: 0.0 for feature_name in SIMILARITY_FEATURE_COLUMNS}

    vector_norm = sqrt(squared_norm)
    return {
        feature_name: component / vector_norm
        for feature_name, component in components.items()
    }


def _shared_similarity_traits(
    anchor_row: dict[str, Any], candidate_row: dict[str, Any], *, limit: int = 3
) -> list[str]:
    ranked: list[tuple[str, float, float]] = []
    for feature_name, label in SIMILARITY_TRAIT_LABELS.items():
        anchor_value = _similarity_feature_value(anchor_row, feature_name)
        candidate_value = _similarity_feature_value(candidate_row, feature_name)
        if anchor_value is None or candidate_value is None:
            continue
        if anchor_value <= 0 or candidate_value <= 0:
            continue
        weight = SIMILARITY_FEATURE_WEIGHTS.get(feature_name, 1.0)
        diff = abs(anchor_value - candidate_value) * weight
        strength = ((anchor_value + candidate_value) / 2.0 * weight) - diff
        if strength <= 0:
            continue
        ranked.append((label, strength, diff))

    ranked.sort(key=lambda item: (item[1], -item[2]), reverse=True)
    return [label for label, _, _ in ranked[:limit]]


def _contrasting_similarity_traits(
    anchor_row: dict[str, Any], candidate_row: dict[str, Any], *, limit: int = 2
) -> list[str]:
    ranked: list[tuple[str, float]] = []
    for feature_name, label in SIMILARITY_TRAIT_LABELS.items():
        anchor_value = _similarity_feature_value(anchor_row, feature_name)
        candidate_value = _similarity_feature_value(candidate_row, feature_name)
        if anchor_value is None or candidate_value is None:
            continue
        difference = abs(
            anchor_value - candidate_value
        ) * SIMILARITY_FEATURE_WEIGHTS.get(feature_name, 1.0)
        if difference < 0.75:
            continue
        ranked.append((label, difference))

    ranked.sort(key=lambda item: item[1], reverse=True)
    return [label for label, _ in ranked[:limit]]


def _trend_direction(status: Any) -> str:
    if status == "rising":
        return "up"
    if status == "falling":
        return "down"
    return "flat"


def build_freshness_payload(
    latest_run: dict[str, Any] | None,
    *,
    now: datetime,
    freshness_threshold_hours: int,
) -> dict[str, Any]:
    checked_at = now.astimezone(UTC).isoformat()
    if latest_run is None:
        return {
            "status": STATE_MISSING,
            "is_fresh": False,
            "checked_at_utc": checked_at,
            "age_hours": None,
            "threshold_hours": freshness_threshold_hours,
            "last_successful_finished_at_utc": None,
        }

    finished_at = _parse_iso_datetime(latest_run.get("finished_at_utc"))
    if finished_at is None:
        return {
            "status": STATE_UNAVAILABLE,
            "is_fresh": False,
            "checked_at_utc": checked_at,
            "age_hours": None,
            "threshold_hours": freshness_threshold_hours,
            "last_successful_finished_at_utc": None,
        }

    age = now.astimezone(UTC) - finished_at
    age_hours = round(age.total_seconds() / 3600, 1)
    is_fresh = age <= timedelta(hours=freshness_threshold_hours)
    return {
        "status": STATE_FRESH if is_fresh else STATE_STALE,
        "is_fresh": is_fresh,
        "checked_at_utc": checked_at,
        "age_hours": age_hours,
        "threshold_hours": freshness_threshold_hours,
        "last_successful_finished_at_utc": finished_at.isoformat(),
    }


def _opportunity_state_from_row(row: dict[str, Any]) -> str:
    if row.get("games_next_7d") in (None, "") and row.get("opportunity_score") in (
        None,
        "",
    ):
        return STATE_UNAVAILABLE
    return STATE_FRESH


def _format_category_profile(row: dict[str, Any]) -> list[dict[str, Any]]:
    categories: list[dict[str, Any]] = []
    for category, field_name in (
        ("PTS", "z_pts"),
        ("REB", "z_reb"),
        ("AST", "z_ast"),
        ("STL", "z_stl"),
        ("BLK", "z_blk"),
        ("3PM", "z_fg3m"),
    ):
        impact = _to_float(row.get(field_name))
        if impact is None:
            continue
        if impact >= 0.75:
            tier = "plus"
        elif impact <= -0.5:
            tier = "minus"
        else:
            tier = "neutral"
        direction = "up" if impact > 0 else "down" if impact < 0 else "flat"
        categories.append(
            {
                "category": category,
                "impact_score": round(impact, 2),
                "category_tier": tier,
                "category_direction": direction,
            }
        )
    categories.sort(key=lambda item: abs(float(item["impact_score"])), reverse=True)
    return categories


def _clamp_percentile(value: float | None) -> float | None:
    if value is None:
        return None
    return min(100.0, max(0.0, value))


def _build_sample_payload(
    source: dict[str, Any], fallback: dict[str, Any] | None = None
) -> dict[str, Any]:
    fallback = fallback or {}
    games_sampled = _to_int(source.get("games_sampled"))
    if games_sampled is None:
        games_sampled = _to_int(fallback.get("games_sampled"))
    qualification_games = _to_int(source.get("qualification_games"))
    if qualification_games is None:
        qualification_games = _to_int(fallback.get("qualification_games")) or 5
    is_qualified = _to_bool(source.get("is_qualified"))
    if is_qualified is None:
        is_qualified = _to_bool(fallback.get("is_qualified"))
    if is_qualified is None and games_sampled is not None:
        is_qualified = games_sampled >= qualification_games
    sample_status = source.get("sample_status") or fallback.get("sample_status")
    if sample_status is None:
        if games_sampled is None:
            sample_status = STATE_UNAVAILABLE
        elif games_sampled >= 10:
            sample_status = "ready"
        elif games_sampled >= qualification_games:
            sample_status = "limited_sample"
        else:
            sample_status = STATE_INSUFFICIENT_SAMPLE
    return {
        "games_sampled": games_sampled,
        "qualification_games": qualification_games,
        "is_qualified": bool(is_qualified),
        "sample_status": sample_status,
        "sample_warning": source.get("sample_warning")
        or fallback.get("sample_warning"),
    }


def _format_stat_percentiles(row: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for config in STAT_PERCENTILE_CONFIG:
        percentile = _clamp_percentile(_to_float(row.get(config["percentile_field"])))
        average = _to_float(row.get(config["average_field"]))
        if percentile is None or average is None:
            continue
        items.append(
            {
                "key": config["key"],
                "label": config["label"],
                "average": round(average, 1),
                "percentile": round(percentile, 1),
                "bar_width": round(percentile, 1),
                "direction": config["direction"],
            }
        )
    return items


def _format_chart_baselines(
    row: dict[str, Any], fallback: dict[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    fallback = fallback or {}
    baselines: dict[str, dict[str, Any]] = {}
    for config in STAT_PERCENTILE_CONFIG:
        value = _to_float(row.get(config["baseline_field"]))
        if value is None:
            value = _to_float(fallback.get(config["baseline_field"]))
        if value is None:
            continue
        baselines[config["key"]] = {
            "key": config["key"],
            "label": config["label"],
            "value": round(value, 1),
            "direction": config["direction"],
        }
    return baselines


def _has_chart_baselines(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    return any(row.get(config["baseline_field"]) is not None for config in STAT_PERCENTILE_CONFIG)


def _format_game_log_row(row: dict[str, Any], game_number: int) -> dict[str, Any]:
    item = dict(row)
    item["game_number"] = game_number
    for key in (
        "pts",
        "reb",
        "ast",
        "stl",
        "blk",
        "tov",
        "fg3m",
        "fgm",
        "fga",
        "ftm",
        "fta",
    ):
        parsed = _to_int(item.get(key))
        if parsed is not None:
            item[key] = parsed
    for key in ("min", "fg_pct", "ft_pct", "fantasy_points_simple"):
        parsed_float = _to_float(item.get(key))
        if parsed_float is not None:
            item[key] = round(parsed_float, 3 if key.endswith("_pct") else 1)
    return item


def _format_trend_row(row: dict[str, Any]) -> dict[str, Any]:
    stat = str(row.get("stat") or "")
    delta = _to_float(row.get("delta"))
    if delta is None:
        direction_is_good = None
    elif stat == "TOV":
        direction_is_good = delta < 0
    else:
        direction_is_good = delta > 0
    return {
        "stat": stat,
        "label": "Box Score Index" if stat == "FANTASY_POINTS_SIMPLE" else stat,
        "recent_games": _to_int(row.get("recent_games")),
        "prior_games": _to_int(row.get("prior_games")),
        "recent_avg": _to_float(row.get("recent_avg")),
        "prior_avg": _to_float(row.get("prior_avg")),
        "delta": delta,
        "pct_change": _to_float(row.get("pct_change")),
        "direction_is_good": direction_is_good,
    }


def _window_state(games_in_window: int | None, expected_games: int | None) -> str:
    if games_in_window is None:
        return STATE_UNAVAILABLE
    if expected_games is not None and games_in_window < expected_games:
        return STATE_INSUFFICIENT_SAMPLE
    return STATE_FRESH


def _window_reason(state: str, window_label: str) -> str | None:
    if state == STATE_INSUFFICIENT_SAMPLE:
        return f"Limited comparison data for {window_label}"
    if state == STATE_UNAVAILABLE:
        return f"No {window_label} data is available yet"
    return None


def _default_recent_form() -> list[dict[str, Any]]:
    windows = (
        ("last_5", "Last 5", 5),
        ("prior_5", "Prior 5", 5),
        ("last_10", "Last 10", 10),
    )
    items: list[dict[str, Any]] = []
    for key, label, expected_games in windows:
        items.append(
            {
                "window_key": key,
                "window_label": label,
                "games_in_window": None,
                "window_games_expected": expected_games,
                "state": STATE_UNAVAILABLE,
                "state_reason": _window_reason(STATE_UNAVAILABLE, label),
                "avg_pts": None,
                "avg_reb": None,
                "avg_ast": None,
                "avg_stl": None,
                "avg_blk": None,
                "avg_fg3m": None,
                "avg_tov": None,
                "avg_minutes": None,
                "fantasy_proxy": None,
            }
        )
    return items


class WarehouseRepository(Protocol):
    def get_dashboard(self, as_of_date: str | None = None) -> dict[str, Any]:
        ...

    def get_leaderboard(self, limit: int = 10) -> list[dict[str, Any]]:
        ...

    def get_trends(self, limit: int = 10) -> list[dict[str, Any]]:
        ...

    def get_recommendations(
        self, limit: int = 10, insight_type: str | None = None
    ) -> list[dict[str, Any]]:
        ...

    def get_rankings(self, limit: int = 25) -> list[dict[str, Any]]:
        ...

    def search_players(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        ...

    def get_player_detail(self, player_id: int) -> dict[str, Any] | None:
        ...

    def get_compare(
        self,
        player_a_id: int,
        player_b_id: int,
        *,
        window: CompareWindow = "last_5",
        focus: CompareFocus = "balanced",
    ) -> dict[str, Any]:
        ...

    def get_latest_analysis(self) -> dict[str, Any] | None:
        ...

    def get_latest_successful_run(self) -> dict[str, Any] | None:
        ...

    def get_player_game_log(
        self,
        player_id: int,
        limit: int = 30,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any] | None:
        ...

    def get_metric_leaders(self, metric: str, limit: int = 10) -> list[dict[str, Any]]:
        ...

    def get_player_metric_percentile(
        self, player_id: int, metric: str, min_games: int = 5
    ) -> dict[str, Any] | None:
        ...

    def get_health(self) -> dict[str, Any]:
        ...


@dataclass
class BigQueryWarehouseRepository:
    settings: Settings
    client: bigquery.Client | None = None

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = bigquery.Client(project=self.settings.project_id or None)

    def _query(
        self, sql: str, params: list[bigquery.ScalarQueryParameter] | None = None
    ) -> list[dict[str, Any]]:
        job_config = None
        if params:
            job_config = bigquery.QueryJobConfig(query_parameters=params)
        result = self.client.query(sql, job_config=job_config).result()
        rows: list[dict[str, Any]] = []
        for row in result:
            rows.append({key: _to_iso(value) for key, value in dict(row).items()})
        return rows

    def _dashboard_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.workbench_dashboard`"

    def _home_dashboard_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.workbench_home_dashboard`"

    def _detail_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.workbench_player_detail`"

    def _agent_player_search_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.agent_dataset}.agent_player_search`"

    def _player_search_index_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.player_search_index`"

    def _compare_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.workbench_compare`"

    def _similarity_feature_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.player_similarity_features`"

    def _archetype_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.player_archetypes`"

    def _dim_player_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.dim_player`"

    def _fct_game_stats_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.fct_player_game_stats`"

    def _player_trends_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.player_trends`"

    def _category_profile_table(self) -> str:
        return f"`{self.settings.project_id}.{self.settings.gold_dataset}.player_category_profile`"

    def _decorate_dashboard_row(self, row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["category_strengths"] = _sanitize_category_list(
            row.get("category_strengths")
        )
        item["category_risks"] = _sanitize_category_list(row.get("category_risks"))
        item["reason_summary"] = build_reason_summary(row)
        item["opportunity_state"] = _opportunity_state_from_row(row)
        item["headshot_url"] = build_headshot_url(row.get("player_id"))
        item["player_initials"] = build_player_initials(row.get("player_name"))
        item["top_improvements"] = _build_top_improvement_chips(row)
        item["trend_direction"] = _trend_direction(row.get("trend_status"))
        return item

    def _decorate_search_player_row(self, row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["player_id"] = _to_int(row.get("player_id"))
        item["games_sampled"] = _to_int(row.get("games_sampled"))
        item["qualification_games"] = _to_int(row.get("qualification_games")) or 5
        item["is_qualified"] = bool(_to_bool(row.get("is_qualified")))
        item["overall_rank"] = _to_int(row.get("overall_rank"))
        item["recommendation_score"] = _to_float(row.get("recommendation_score"))
        item["headshot_url"] = build_headshot_url(row.get("player_id"))
        item["player_initials"] = build_player_initials(row.get("player_name"))
        return item

    def _fetch_dashboard_rows(
        self,
        *,
        limit: int,
        order_by: str,
        where_clause: str = "",
        extra_params: list[bigquery.ScalarQueryParameter] | None = None,
    ) -> list[dict[str, Any]]:
        params: list[bigquery.ScalarQueryParameter] = [
            bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
        if extra_params:
            params.extend(extra_params)
        sql = f"""
        SELECT
          season,
          as_of_date,
          player_id,
          player_name,
          latest_team_abbr,
          latest_game_date,
          overall_rank,
          recommendation_score,
          recommendation_tier,
          category_strengths,
          category_risks,
          last_5_games,
          prior_5_games,
          last_10_games,
          fantasy_proxy_last_5,
          fantasy_proxy_prior_5,
          fantasy_proxy_last_10,
          trend_delta,
          trend_pct_change,
          trend_status,
          next_game_date,
          next_opponent_abbr,
          games_next_7d,
          back_to_backs_next_7d,
          opportunity_score,
          reason_primary_code,
          reason_primary_value,
          reason_secondary_code,
          reason_secondary_value,
          reason_context_code,
          reason_context_value
        FROM {self._dashboard_table()}
        WHERE season = @season
          {where_clause}
        ORDER BY {order_by}
        LIMIT @limit
        """
        return [self._decorate_dashboard_row(row) for row in self._query(sql, params)]

    def _fetch_home_date_options(self) -> list[str]:
        sql = f"""
        SELECT DISTINCT as_of_date
        FROM {self._home_dashboard_table()}
        WHERE season = @season
        ORDER BY as_of_date DESC
        LIMIT 7
        """
        try:
            return [
                str(row["as_of_date"])
                for row in self._query(
                    sql,
                    [
                        bigquery.ScalarQueryParameter(
                            "season", "STRING", SUPPORTED_SEASON
                        )
                    ],
                )
            ]
        except BQAPIError:
            return []

    def _resolve_home_as_of_date(
        self, requested: str | None
    ) -> tuple[str | None, list[str]]:
        options = self._fetch_home_date_options()
        if not options:
            return None, []
        if requested and requested in options:
            return requested, options
        parsed_requested = _parse_iso_date(requested)
        if parsed_requested is not None:
            normalized = parsed_requested.isoformat()
            if normalized in options:
                return normalized, options
        return options[0], options

    def _fetch_home_dashboard_rows(
        self,
        *,
        as_of_date: str,
        limit: int,
        order_by: str,
        where_clause: str = "",
    ) -> list[dict[str, Any]]:
        parsed_as_of_date = _parse_iso_date(as_of_date)
        params: list[bigquery.ScalarQueryParameter] = [
            bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
            bigquery.ScalarQueryParameter("as_of_date", "DATE", parsed_as_of_date),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
        sql = f"""
        SELECT
          season,
          as_of_date,
          player_id,
          player_name,
          latest_team_abbr,
          latest_game_date,
          overall_rank,
          recommendation_score,
          recommendation_tier,
          category_strengths,
          category_risks,
          last_5_games,
          prior_5_games,
          last_10_games,
          fantasy_proxy_last_5,
          fantasy_proxy_prior_5,
          fantasy_proxy_last_10,
          trend_delta,
          trend_pct_change,
          trend_status,
          next_game_date,
          next_opponent_abbr,
          games_next_7d,
          back_to_backs_next_7d,
          opportunity_score,
          pts_delta,
          reb_delta,
          ast_delta,
          stl_delta,
          blk_delta,
          fg3m_delta,
          min_delta,
          reason_primary_code,
          reason_primary_value,
          reason_secondary_code,
          reason_secondary_value,
          reason_context_code,
          reason_context_value
        FROM {self._home_dashboard_table()}
        WHERE season = @season
          AND as_of_date = @as_of_date
          {where_clause}
        ORDER BY {order_by}
        LIMIT @limit
        """
        return [self._decorate_dashboard_row(row) for row in self._query(sql, params)]

    def _fetch_player_identity_from_search_table(
        self, table: str, player_id: int
    ) -> list[dict[str, Any]]:
        sql = f"""
        SELECT
          player_id,
          player_name,
          latest_season,
          latest_team_abbr,
          latest_game_date,
          games_sampled,
          qualification_games,
          is_qualified,
          sample_status,
          sample_warning,
          overall_rank,
          recommendation_score,
          last_seen_at_utc
        FROM {table}
        WHERE latest_season = @season
          AND player_id = @player_id
        LIMIT 1
        """
        params = [
            bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
            bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
        ]
        return self._query(sql, params)

    def _fetch_player_identity(self, player_id: int) -> dict[str, Any] | None:
        try:
            rows = self._fetch_player_identity_from_search_table(
                self._agent_player_search_table(), player_id
            )
        except BQAPIError:
            try:
                rows = self._fetch_player_identity_from_search_table(
                    self._player_search_index_table(), player_id
                )
            except BQAPIError:
                rows = self._fetch_player_identity_from_game_stats(player_id)
        return rows[0] if rows else None

    def _fetch_player_identity_from_game_stats(
        self, player_id: int
    ) -> list[dict[str, Any]]:
        sql = f"""
        WITH qualified AS (
          SELECT
            season,
            player_id,
            ANY_VALUE(player_name) AS player_name,
            ARRAY_AGG(
              team_abbr IGNORE NULLS
              ORDER BY game_date DESC, ingested_at_utc DESC
              LIMIT 1
            )[SAFE_OFFSET(0)] AS latest_team_abbr,
            MAX(game_date) AS latest_game_date,
            COUNT(*) AS games_sampled,
            MAX(ingested_at_utc) AS last_seen_at_utc
          FROM {self._fct_game_stats_table()}
          WHERE season = @season
            AND player_id = @player_id
          GROUP BY season, player_id
          HAVING COUNT(*) >= 5
        )
        SELECT
          player_id,
          player_name,
          season AS latest_season,
          latest_team_abbr,
          latest_game_date,
          games_sampled,
          5 AS qualification_games,
          TRUE AS is_qualified,
          CASE
            WHEN games_sampled >= 10 THEN 'ready'
            ELSE 'limited_sample'
          END AS sample_status,
          CASE
            WHEN games_sampled >= 10 THEN NULL
            ELSE 'Limited sample: percentiles are available after the dbt model is rebuilt.'
          END AS sample_warning,
          NULL AS overall_rank,
          NULL AS recommendation_score,
          last_seen_at_utc
        FROM qualified
        LIMIT 1
        """
        try:
            return self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                    bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
                ],
            )
        except BQAPIError:
            return []

    def _similarity_distance_sql(self, anchor_alias: str, candidate_alias: str) -> str:
        terms = []
        for feature_name in SIMILARITY_FEATURE_COLUMNS:
            weight = SIMILARITY_FEATURE_WEIGHTS.get(feature_name, 1.0)
            anchor_component = (
                f"COALESCE(SAFE_DIVIDE("
                f"COALESCE({anchor_alias}.norm_{feature_name}, 0) * {weight}, "
                f"NULLIF({anchor_alias}.similarity_vector_norm, 0)), 0)"
            )
            candidate_component = (
                f"COALESCE(SAFE_DIVIDE("
                f"COALESCE({candidate_alias}.norm_{feature_name}, 0) * {weight}, "
                f"NULLIF({candidate_alias}.similarity_vector_norm, 0)), 0)"
            )
            terms.append(f"POW({candidate_component} - {anchor_component}, 2)")
        return " + ".join(terms)

    def _similarity_vector_norm_sql(self, alias: str) -> str:
        terms = []
        for feature_name in SIMILARITY_FEATURE_COLUMNS:
            weight = SIMILARITY_FEATURE_WEIGHTS.get(feature_name, 1.0)
            terms.append(f"POW(COALESCE({alias}.norm_{feature_name}, 0) * {weight}, 2)")
        return f"SQRT({' + '.join(terms)})"

    def _fetch_similarity_anchor(self, player_id: int) -> dict[str, Any] | None:
        normalized_fields = ",\n          ".join(
            [f"norm_{feature_name}" for feature_name in SIMILARITY_FEATURE_COLUMNS]
        )
        sql = f"""
        SELECT
          season,
          as_of_date,
          player_id,
          player_name,
          team_abbr,
          position,
          games_sampled,
          sample_status,
          archetype_id,
          archetype_label,
          cluster_confidence,
          top_traits,
          contrasting_traits,
          archetype_summary,
          {normalized_fields}
        FROM {self._similarity_feature_table()}
        WHERE season = @season
          AND player_id = @player_id
        LIMIT 1
        """
        try:
            rows = self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                    bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
                ],
            )
        except BQAPIError:
            return None
        return rows[0] if rows else None

    def _get_similar_players(
        self,
        player_id: int,
        *,
        anchor: dict[str, Any] | None = None,
        limit: int = SIMILARITY_RESULT_LIMIT,
    ) -> tuple[str, str | None, list[dict[str, Any]]]:
        if anchor is None:
            anchor = self._fetch_similarity_anchor(player_id)
        if anchor is None:
            return STATE_UNAVAILABLE, "Similarity profile is unavailable.", []

        anchor_state = _similarity_state_from_sample_status(anchor.get("sample_status"))
        if anchor_state == STATE_INSUFFICIENT_SAMPLE:
            return (
                anchor_state,
                "Not enough games are available to generate similar players yet.",
                [],
            )

        normalized_fields = ",\n          ".join(
            [
                f"candidate.norm_{feature_name}"
                for feature_name in SIMILARITY_FEATURE_COLUMNS
            ]
        )
        distance_sql = self._similarity_distance_sql("anchor", "candidate")
        sql = f"""
        WITH anchor_raw AS (
          SELECT *
          FROM {self._similarity_feature_table()}
          WHERE season = @season
            AND player_id = @player_id
          LIMIT 1
        ),
        anchor AS (
          SELECT
            anchor_raw.*,
            {self._similarity_vector_norm_sql("anchor_raw")} AS similarity_vector_norm
          FROM anchor_raw
        ),
        candidate_pool AS (
          SELECT
            candidate.*,
            {self._similarity_vector_norm_sql("candidate")} AS similarity_vector_norm
          FROM {self._similarity_feature_table()} candidate
          WHERE candidate.season = @season
            AND candidate.sample_status IN ('ready', 'limited_sample')
        ),
        scored AS (
          SELECT
            candidate.player_id,
            candidate.player_name,
            candidate.team_abbr,
            candidate.archetype_label,
            candidate.cluster_confidence,
            candidate.top_traits,
            candidate.contrasting_traits,
            candidate.sample_status,
            {normalized_fields},
            SQRT({distance_sql}) AS euclidean_distance
          FROM candidate_pool candidate
          CROSS JOIN anchor
          WHERE candidate.player_id != anchor.player_id
        )
        SELECT
          *,
          ROUND(1 / (1 + euclidean_distance), 4) AS similarity_score
        FROM scored
        ORDER BY euclidean_distance ASC, player_name
        LIMIT @limit
        """
        try:
            rows = self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                    bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
                    bigquery.ScalarQueryParameter("limit", "INT64", limit),
                ],
            )
        except BQAPIError:
            return STATE_UNAVAILABLE, "Similarity profile is unavailable.", []

        items: list[dict[str, Any]] = []
        for row in rows:
            shared_traits = _shared_similarity_traits(anchor, row)
            if not shared_traits:
                shared_traits = [
                    trait
                    for trait in _split_display_list(anchor.get("top_traits"))
                    if trait in _split_display_list(row.get("top_traits"))
                ][:3]
            items.append(
                {
                    "player_id": _to_int(row.get("player_id")),
                    "player_name": row.get("player_name"),
                    "team_abbr": row.get("team_abbr"),
                    "headshot_url": build_headshot_url(row.get("player_id")),
                    "player_initials": build_player_initials(row.get("player_name")),
                    "similarity_score": _to_float(row.get("similarity_score")),
                    "archetype_label": row.get("archetype_label"),
                    "shared_traits": shared_traits,
                    "contrasting_traits": _contrasting_similarity_traits(anchor, row),
                }
            )

        if not items:
            return STATE_UNAVAILABLE, "No similar-player matches are available.", []
        return STATE_FRESH, None, items

    def _get_pair_similarity(
        self, player_a_id: int, player_b_id: int
    ) -> dict[str, Any]:
        player_a = self._fetch_similarity_anchor(player_a_id)
        player_b = self._fetch_similarity_anchor(player_b_id)
        if player_a is None or player_b is None:
            return {
                "state": STATE_UNAVAILABLE,
                "score": None,
                "summary": "Similarity profile is unavailable for at least one player.",
                "same_archetype": False,
                "archetype_labels": [],
                "shared_traits": [],
                "contrasting_traits": [],
            }

        state_a = _similarity_state_from_sample_status(player_a.get("sample_status"))
        state_b = _similarity_state_from_sample_status(player_b.get("sample_status"))
        if STATE_INSUFFICIENT_SAMPLE in (state_a, state_b):
            return {
                "state": STATE_INSUFFICIENT_SAMPLE,
                "score": None,
                "summary": "One player does not have enough games for a stable similarity read yet.",
                "same_archetype": False,
                "archetype_labels": [],
                "shared_traits": [],
                "contrasting_traits": [],
            }

        player_a_vector = _weighted_similarity_vector(player_a)
        player_b_vector = _weighted_similarity_vector(player_b)
        squared_distance = 0.0
        for feature_name in SIMILARITY_FEATURE_COLUMNS:
            squared_distance += (
                player_b_vector[feature_name] - player_a_vector[feature_name]
            ) ** 2
        score = round(1 / (1 + sqrt(squared_distance)), 4)
        if score is None:
            return {
                "state": STATE_UNAVAILABLE,
                "score": None,
                "summary": "Similarity profile is unavailable for at least one player.",
                "same_archetype": False,
                "archetype_labels": [],
                "shared_traits": [],
                "contrasting_traits": [],
            }
        same_archetype = player_a.get("archetype_label") is not None and player_a.get(
            "archetype_label"
        ) == player_b.get("archetype_label")
        if same_archetype:
            summary = (
                f"Shared archetype: {player_a.get('archetype_label')}. "
                f"Current stat-profile similarity is {score}."
            )
        else:
            summary = (
                f"Archetypes diverge: {player_a.get('archetype_label')} vs "
                f"{player_b.get('archetype_label')}. Current stat-profile similarity is {score}."
            )
        return {
            "state": STATE_FRESH,
            "score": score,
            "summary": summary,
            "same_archetype": same_archetype,
            "archetype_labels": [
                player_a.get("archetype_label"),
                player_b.get("archetype_label"),
            ],
            "shared_traits": _shared_similarity_traits(player_a, player_b),
            "contrasting_traits": _contrasting_similarity_traits(player_a, player_b),
        }

    def _fetch_player_game_log_payload(
        self,
        identity: dict[str, Any],
        limit: int = 30,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        player_id = _to_int(identity.get("player_id"))
        filters = ["season = @season", "player_id = @player_id"]
        params: list[bigquery.ScalarQueryParameter] = [
            bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
            bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
        parsed_start_date = _parse_iso_date(start_date)
        parsed_end_date = _parse_iso_date(end_date)
        if parsed_start_date is not None:
            filters.append("game_date >= @start_date")
            params.append(
                bigquery.ScalarQueryParameter("start_date", "DATE", parsed_start_date)
            )
        if parsed_end_date is not None:
            filters.append("game_date <= @end_date")
            params.append(
                bigquery.ScalarQueryParameter("end_date", "DATE", parsed_end_date)
            )
        sql = f"""
        SELECT
          game_id,
          season,
          game_date,
          player_id,
          player_name,
          team_abbr,
          opponent_abbr,
          home_away,
          matchup,
          wl,
          min,
          pts,
          reb,
          ast,
          stl,
          blk,
          tov,
          fg3m,
          fgm,
          fga,
          fg_pct,
          ftm,
          fta,
          ft_pct,
          fantasy_points_simple
        FROM {self._fct_game_stats_table()}
        WHERE {" AND ".join(filters)}
        ORDER BY game_date DESC, game_id DESC
        LIMIT @limit
        """
        try:
            rows = self._query(sql, params)
        except BQAPIError:
            rows = []
        rows.reverse()
        games = [
            _format_game_log_row(row, game_number=index + 1)
            for index, row in enumerate(rows)
        ]
        return {
            "player_id": player_id,
            "player_name": identity.get("player_name"),
            "season": SUPPORTED_SEASON,
            "games": games,
            "games_returned": len(games),
            "limit": limit,
            "order": "chronological",
            "date_range": {
                "start_date": parsed_start_date.isoformat()
                if parsed_start_date is not None
                else None,
                "end_date": parsed_end_date.isoformat()
                if parsed_end_date is not None
                else None,
            },
        }

    def _fetch_player_trends(self, player_id: int) -> list[dict[str, Any]]:
        sql = f"""
        SELECT
          stat,
          recent_games,
          prior_games,
          recent_avg,
          prior_avg,
          delta,
          pct_change
        FROM {self._player_trends_table()}
        WHERE season = @season
          AND player_id = @player_id
          AND stat IN ('PTS', 'REB', 'AST', 'STL', 'BLK', 'TOV', 'MIN', 'FANTASY_POINTS_SIMPLE')
        """
        try:
            rows = self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                    bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
                ],
            )
        except BQAPIError:
            return []
        rows.sort(key=lambda item: TREND_STAT_ORDER.get(str(item.get("stat")), 99))
        return [_format_trend_row(row) for row in rows]

    def _fetch_chart_baseline_row(self) -> dict[str, Any]:
        sql = f"""
        WITH player_means AS (
          SELECT
            season,
            player_id,
            AVG(pts) AS avg_pts,
            AVG(reb) AS avg_reb,
            AVG(ast) AS avg_ast,
            AVG(stl) AS avg_stl,
            AVG(blk) AS avg_blk,
            AVG(tov) AS avg_tov,
            COUNT(*) AS games_sampled
          FROM {self._fct_game_stats_table()}
          WHERE season = @season
          GROUP BY season, player_id
          HAVING COUNT(*) >= 5
        )
        SELECT
          ROUND(AVG(avg_pts), 2) AS league_avg_pts,
          ROUND(AVG(avg_reb), 2) AS league_avg_reb,
          ROUND(AVG(avg_ast), 2) AS league_avg_ast,
          ROUND(AVG(avg_stl), 2) AS league_avg_stl,
          ROUND(AVG(avg_blk), 2) AS league_avg_blk,
          ROUND(AVG(avg_tov), 2) AS league_avg_tov
        FROM player_means
        WHERE season = @season
        """
        try:
            rows = self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON)
                ],
            )
        except BQAPIError:
            return {}
        return rows[0] if rows else {}

    def _fetch_player_detail_row(
        self, player_id: int
    ) -> dict[str, Any] | None:
        params = [
            bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
            bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
        ]
        sql = f"""
        SELECT
          season,
          as_of_date,
          player_id,
          player_name,
          latest_team_abbr,
          latest_game_date,
          overall_rank,
          recommendation_score,
          recommendation_tier,
          category_strengths,
          category_risks,
          trend_delta,
          trend_pct_change,
          trend_status,
          next_game_date,
          next_opponent_abbr,
          games_next_7d,
          back_to_backs_next_7d,
          opportunity_score,
          reason_primary_code,
          reason_primary_value,
          reason_secondary_code,
          reason_secondary_value,
          reason_context_code,
          reason_context_value,
          games_sampled,
          qualification_games,
          is_qualified,
          sample_status,
          sample_warning,
          z_pts,
          z_reb,
          z_ast,
          z_stl,
          z_blk,
          z_fg3m,
          z_tov,
          pts_percentile,
          reb_percentile,
          ast_percentile,
          stl_percentile,
          blk_percentile,
          tov_percentile,
          season_avg_pts,
          season_avg_reb,
          season_avg_ast,
          season_avg_stl,
          season_avg_blk,
          season_avg_tov,
          league_avg_pts,
          league_avg_reb,
          league_avg_ast,
          league_avg_stl,
          league_avg_blk,
          league_avg_tov,
          category_score_7cat,
          category_coverage_status,
          last_5_games,
          last_5_avg_min,
          last_5_avg_pts,
          last_5_avg_reb,
          last_5_avg_ast,
          last_5_avg_stl,
          last_5_avg_blk,
          last_5_avg_fg3m,
          last_5_avg_tov,
          last_5_fantasy_proxy,
          prior_5_games,
          prior_5_avg_min,
          prior_5_avg_pts,
          prior_5_avg_reb,
          prior_5_avg_ast,
          prior_5_avg_stl,
          prior_5_avg_blk,
          prior_5_avg_fg3m,
          prior_5_avg_tov,
          prior_5_fantasy_proxy,
          last_10_games,
          last_10_avg_min,
          last_10_avg_pts,
          last_10_avg_reb,
          last_10_avg_ast,
          last_10_avg_stl,
          last_10_avg_blk,
          last_10_avg_fg3m,
          last_10_avg_tov,
          last_10_fantasy_proxy
        FROM {self._detail_table()}
        WHERE season = @season
          AND player_id = @player_id
        LIMIT 1
        """
        try:
            rows = self._query(sql, params)
        except BQAPIError:
            rows = self._fetch_legacy_player_detail_row(player_id)
        return rows[0] if rows else None

    def _fetch_legacy_player_detail_row(
        self, player_id: int
    ) -> list[dict[str, Any]]:
        sql = f"""
        SELECT
          season,
          as_of_date,
          player_id,
          player_name,
          latest_team_abbr,
          latest_game_date,
          overall_rank,
          recommendation_score,
          recommendation_tier,
          category_strengths,
          category_risks,
          trend_delta,
          trend_pct_change,
          trend_status,
          next_game_date,
          next_opponent_abbr,
          games_next_7d,
          back_to_backs_next_7d,
          opportunity_score,
          reason_primary_code,
          reason_primary_value,
          reason_secondary_code,
          reason_secondary_value,
          reason_context_code,
          reason_context_value,
          z_pts,
          z_reb,
          z_ast,
          z_stl,
          z_blk,
          z_fg3m,
          z_tov,
          category_score_7cat,
          category_coverage_status,
          last_5_games,
          last_5_avg_min,
          last_5_avg_pts,
          last_5_avg_reb,
          last_5_avg_ast,
          last_5_avg_stl,
          last_5_avg_blk,
          last_5_avg_fg3m,
          last_5_avg_tov,
          last_5_fantasy_proxy,
          prior_5_games,
          prior_5_avg_min,
          prior_5_avg_pts,
          prior_5_avg_reb,
          prior_5_avg_ast,
          prior_5_avg_stl,
          prior_5_avg_blk,
          prior_5_avg_fg3m,
          prior_5_avg_tov,
          prior_5_fantasy_proxy,
          last_10_games,
          last_10_avg_min,
          last_10_avg_pts,
          last_10_avg_reb,
          last_10_avg_ast,
          last_10_avg_stl,
          last_10_avg_blk,
          last_10_avg_fg3m,
          last_10_avg_tov,
          last_10_fantasy_proxy
        FROM {self._detail_table()}
        WHERE season = @season
          AND player_id = @player_id
        LIMIT 1
        """
        try:
            return self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                    bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
                ],
            )
        except BQAPIError:
            return []

    def _build_player_detail_payload(
        self,
        *,
        identity: dict[str, Any],
        row: dict[str, Any] | None,
        archetype_row: dict[str, Any] | None,
        similarity_state: str,
        similarity_reason: str | None,
        similar_players: list[dict[str, Any]],
        game_log: dict[str, Any],
        trends: list[dict[str, Any]],
        chart_baselines: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        archetype_state = STATE_UNAVAILABLE
        archetype_payload = {
            "state": STATE_UNAVAILABLE,
            "archetype_id": None,
            "archetype_label": None,
            "cluster_confidence": None,
            "top_traits": [],
            "summary": None,
        }
        if archetype_row is not None:
            archetype_state = _similarity_state_from_sample_status(
                archetype_row.get("sample_status")
            )
            archetype_payload = {
                "state": archetype_state,
                "archetype_id": archetype_row.get("archetype_id"),
                "archetype_label": archetype_row.get("archetype_label"),
                "cluster_confidence": _to_float(
                    archetype_row.get("cluster_confidence")
                ),
                "top_traits": _split_display_list(archetype_row.get("top_traits")),
                "summary": archetype_row.get("archetype_summary"),
            }

        game_log_state = (
            STATE_FRESH if game_log.get("games") else STATE_UNAVAILABLE
        )
        trends_state = STATE_FRESH if trends else STATE_UNAVAILABLE

        if row is None:
            sample = _build_sample_payload(identity)
            return {
                "player": {
                    "season": identity.get("latest_season", SUPPORTED_SEASON),
                    "player_id": identity.get("player_id"),
                    "player_name": identity.get("player_name"),
                    "headshot_url": build_headshot_url(identity.get("player_id")),
                    "player_initials": build_player_initials(
                        identity.get("player_name")
                    ),
                    "team_abbr": identity.get("latest_team_abbr"),
                    "latest_game_date": identity.get("latest_game_date"),
                    "overall_rank": _to_int(identity.get("overall_rank")),
                    "recommendation_score": _to_float(
                        identity.get("recommendation_score")
                    ),
                    "recommendation_tier": None,
                    "category_strengths": None,
                    "category_risks": None,
                    "is_ranked": False,
                    "games_sampled": sample["games_sampled"],
                    "sample_status": sample["sample_status"],
                    "is_qualified": sample["is_qualified"],
                },
                "sample": sample,
                "availability_state": STATE_UNAVAILABLE,
                "availability_reason": "Not currently ranked",
                "reason_summary": None,
                "trend": {
                    "status": STATE_UNAVAILABLE,
                    "delta": None,
                    "pct_change": None,
                },
                "panel_states": {
                    "recent_form": STATE_UNAVAILABLE,
                    "category_profile": STATE_UNAVAILABLE,
                    "stat_percentiles": STATE_UNAVAILABLE,
                    "game_log": game_log_state,
                    "trends": trends_state,
                    "opportunity": STATE_UNAVAILABLE,
                    "archetype": archetype_state,
                    "similarity": similarity_state,
                },
                "recent_form": _default_recent_form(),
                "category_profile": [],
                "stat_percentiles": [],
                "chart_baselines": chart_baselines,
                "game_log": game_log,
                "trends": trends,
                "opportunity": None,
                "archetype": archetype_payload,
                "similarity_reason": similarity_reason,
                "similar_players": similar_players,
            }

        recent_form = []
        for key, label, expected_games in (
            ("last_5", "Last 5", 5),
            ("prior_5", "Prior 5", 5),
            ("last_10", "Last 10", 10),
        ):
            games_in_window = _to_int(row.get(f"{key}_games"))
            state = _window_state(games_in_window, expected_games)
            recent_form.append(
                {
                    "window_key": key,
                    "window_label": label,
                    "games_in_window": games_in_window,
                    "window_games_expected": expected_games,
                    "state": state,
                    "state_reason": _window_reason(state, label),
                    "avg_pts": row.get(f"{key}_avg_pts"),
                    "avg_reb": row.get(f"{key}_avg_reb"),
                    "avg_ast": row.get(f"{key}_avg_ast"),
                    "avg_stl": row.get(f"{key}_avg_stl"),
                    "avg_blk": row.get(f"{key}_avg_blk"),
                    "avg_fg3m": row.get(f"{key}_avg_fg3m"),
                    "avg_tov": row.get(f"{key}_avg_tov"),
                    "avg_minutes": row.get(f"{key}_avg_min"),
                    "fantasy_proxy": row.get(f"{key}_fantasy_proxy"),
                }
            )

        category_profile = _format_category_profile(row)
        stat_percentiles = _format_stat_percentiles(row)
        sample = _build_sample_payload(row, identity)
        opportunity_state = _opportunity_state_from_row(row)
        recent_form_state = (
            STATE_FRESH
            if any(item["state"] == STATE_FRESH for item in recent_form)
            else STATE_INSUFFICIENT_SAMPLE
            if any(item["state"] == STATE_INSUFFICIENT_SAMPLE for item in recent_form)
            else STATE_UNAVAILABLE
        )
        category_profile_state = STATE_FRESH if category_profile else STATE_UNAVAILABLE
        stat_percentiles_state = (
            STATE_FRESH if stat_percentiles else STATE_UNAVAILABLE
        )
        opportunity = None
        if opportunity_state != STATE_UNAVAILABLE:
            opportunity = {
                "games_next_7d": row.get("games_next_7d"),
                "back_to_backs_next_7d": row.get("back_to_backs_next_7d"),
                "next_opponent": row.get("next_opponent_abbr"),
                "next_game_date": row.get("next_game_date"),
                "opportunity_score": row.get("opportunity_score"),
            }

        return {
            "player": {
                "season": row.get("season"),
                "player_id": row.get("player_id"),
                "player_name": row.get("player_name"),
                "headshot_url": build_headshot_url(row.get("player_id")),
                "player_initials": build_player_initials(row.get("player_name")),
                "team_abbr": row.get("latest_team_abbr"),
                "latest_game_date": row.get("latest_game_date"),
                "overall_rank": row.get("overall_rank"),
                "recommendation_score": row.get("recommendation_score"),
                "recommendation_tier": row.get("recommendation_tier"),
                "category_strengths": _sanitize_category_list(
                    row.get("category_strengths")
                ),
                "category_risks": _sanitize_category_list(row.get("category_risks")),
                "is_ranked": row.get("overall_rank") is not None,
                "games_sampled": sample["games_sampled"],
                "sample_status": sample["sample_status"],
                "is_qualified": sample["is_qualified"],
            },
            "sample": sample,
            "availability_state": (
                STATE_FRESH
                if row.get("overall_rank") is not None
                else STATE_UNAVAILABLE
            ),
            "availability_reason": (
                None if row.get("overall_rank") is not None else "Not currently ranked"
            ),
            "reason_summary": build_reason_summary(row),
            "trend": {
                "status": row.get("trend_status"),
                "delta": row.get("trend_delta"),
                "pct_change": row.get("trend_pct_change"),
            },
            "panel_states": {
                "recent_form": recent_form_state,
                "category_profile": category_profile_state,
                "stat_percentiles": stat_percentiles_state,
                "game_log": game_log_state,
                "trends": trends_state,
                "opportunity": opportunity_state,
                "archetype": archetype_state,
                "similarity": similarity_state,
            },
            "recent_form": recent_form,
            "category_profile": category_profile,
            "stat_percentiles": stat_percentiles,
            "chart_baselines": chart_baselines,
            "game_log": game_log,
            "trends": trends,
            "opportunity": opportunity,
            "archetype": archetype_payload,
            "similarity_reason": similarity_reason,
            "similar_players": similar_players,
        }

    def get_dashboard(self, as_of_date: str | None = None) -> dict[str, Any]:
        selected_as_of_date, date_options = self._resolve_home_as_of_date(as_of_date)
        if selected_as_of_date is None:
            return {
                "selected_as_of_date": None,
                "date_options": [],
                "signals": [],
                "rankings": [],
                "trends": [],
                "opportunity": [],
            }

        rows = self._fetch_home_dashboard_rows(
            as_of_date=selected_as_of_date,
            limit=18,
            order_by="recommendation_score DESC, overall_rank ASC, player_name",
        )
        signals = rows[:6]
        rankings = sorted(
            [item for item in rows if item.get("overall_rank") is not None],
            key=lambda item: int(item["overall_rank"]),
        )[:8]
        trends = sorted(
            rows,
            key=lambda item: abs(_to_float(item.get("trend_delta")) or 0.0),
            reverse=True,
        )[:6]
        opportunity = [
            item for item in rows if item["opportunity_state"] != STATE_UNAVAILABLE
        ][:6]
        return {
            "selected_as_of_date": selected_as_of_date,
            "date_options": [
                {
                    "value": value,
                    "label": _format_home_date_label(value),
                    "is_selected": value == selected_as_of_date,
                }
                for value in date_options
            ],
            "signals": signals,
            "rankings": rankings,
            "trends": trends,
            "opportunity": opportunity,
        }

    def get_leaderboard(self, limit: int = 10) -> list[dict[str, Any]]:
        table = f"`{self.settings.project_id}.{self.settings.gold_dataset}.daily_leaderboard`"
        sql = f"""
        SELECT
          season,
          game_date,
          pts_leader,
          pts_matchup,
          pts,
          reb_leader,
          reb,
          ast_leader,
          ast
        FROM {table}
        WHERE season = @season
        ORDER BY game_date DESC, pts DESC, pts_leader
        LIMIT @limit
        """
        return self._query(
            sql,
            [
                bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                bigquery.ScalarQueryParameter("limit", "INT64", limit),
            ],
        )

    def get_trends(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._fetch_dashboard_rows(
            limit=limit,
            order_by="ABS(trend_delta) DESC, player_name",
        )

    def get_recommendations(
        self, limit: int = 10, insight_type: str | None = None
    ) -> list[dict[str, Any]]:
        table = f"`{self.settings.project_id}.{self.settings.gold_dataset}.fantasy_insights`"
        filters = ["season = @season"]
        params: list[bigquery.ScalarQueryParameter] = [
            bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
        if insight_type:
            filters.append("insight_type = @insight_type")
            params.append(
                bigquery.ScalarQueryParameter("insight_type", "STRING", insight_type)
            )
        sql = f"""
        SELECT
          insight_id,
          as_of_date,
          player_id,
          player_name,
          insight_type,
          priority_score,
          confidence_score,
          category_focus,
          recommendation,
          title,
          summary,
          evidence_json,
          source_label
        FROM {table}
        WHERE {" AND ".join(filters)}
        ORDER BY as_of_date DESC, priority_score DESC, confidence_score DESC, player_name
        LIMIT @limit
        """
        return self._query(sql, params)

    def get_rankings(self, limit: int = 25) -> list[dict[str, Any]]:
        return self._fetch_dashboard_rows(
            limit=limit,
            order_by="overall_rank ASC, recommendation_score DESC, player_name",
        )

    def _search_players_from_search_table(
        self, table: str, query: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        sql = f"""
        SELECT
          player_id,
          player_name,
          latest_season,
          latest_team_abbr,
          latest_game_date,
          games_sampled,
          qualification_games,
          is_qualified,
          sample_status,
          sample_warning,
          overall_rank,
          recommendation_score,
          last_seen_at_utc
        FROM {table}
        WHERE latest_season = @season
          AND search_text LIKE CONCAT('%', LOWER(@query), '%')
        ORDER BY
          CASE
            WHEN LOWER(player_name) = LOWER(@query) THEN 0
            WHEN STARTS_WITH(LOWER(player_name), LOWER(@query)) THEN 1
            ELSE 2
          END,
          overall_rank IS NULL,
          overall_rank,
          player_name
        LIMIT @limit
        """
        params = [
            bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
            bigquery.ScalarQueryParameter("query", "STRING", query.strip()),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
        return self._query(sql, params)

    def search_players(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        try:
            rows = self._search_players_from_search_table(
                self._agent_player_search_table(), query, limit
            )
        except BQAPIError:
            try:
                rows = self._search_players_from_search_table(
                    self._player_search_index_table(), query, limit
                )
            except BQAPIError:
                rows = self._search_players_from_game_stats(query, limit=limit)
        return [self._decorate_search_player_row(row) for row in rows]

    def _search_players_from_game_stats(
        self, query: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        sql = f"""
        WITH qualified AS (
          SELECT
            season,
            player_id,
            ANY_VALUE(player_name) AS player_name,
            ARRAY_AGG(
              team_abbr IGNORE NULLS
              ORDER BY game_date DESC, ingested_at_utc DESC
              LIMIT 1
            )[SAFE_OFFSET(0)] AS latest_team_abbr,
            MAX(game_date) AS latest_game_date,
            COUNT(*) AS games_sampled,
            MAX(ingested_at_utc) AS last_seen_at_utc
          FROM {self._fct_game_stats_table()}
          WHERE season = @season
          GROUP BY season, player_id
          HAVING COUNT(*) >= 5
        )
        SELECT
          player_id,
          player_name,
          season AS latest_season,
          latest_team_abbr,
          latest_game_date,
          games_sampled,
          5 AS qualification_games,
          TRUE AS is_qualified,
          CASE
            WHEN games_sampled >= 10 THEN 'ready'
            ELSE 'limited_sample'
          END AS sample_status,
          CASE
            WHEN games_sampled >= 10 THEN NULL
            ELSE 'Limited sample: percentiles are available after the dbt model is rebuilt.'
          END AS sample_warning,
          NULL AS overall_rank,
          NULL AS recommendation_score,
          last_seen_at_utc
        FROM qualified
        WHERE LOWER(player_name) LIKE CONCAT('%', LOWER(@query), '%')
        ORDER BY
          CASE
            WHEN LOWER(player_name) = LOWER(@query) THEN 0
            WHEN STARTS_WITH(LOWER(player_name), LOWER(@query)) THEN 1
            ELSE 2
          END,
          games_sampled DESC,
          player_name
        LIMIT @limit
        """
        try:
            return self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                    bigquery.ScalarQueryParameter("query", "STRING", query.strip()),
                    bigquery.ScalarQueryParameter("limit", "INT64", limit),
                ],
            )
        except BQAPIError:
            return []

    def get_player_detail(self, player_id: int) -> dict[str, Any] | None:
        identity = self._fetch_player_identity(player_id)
        if identity is None:
            return None

        row = self._fetch_player_detail_row(player_id)
        game_log = self._fetch_player_game_log_payload(identity, limit=30)
        trends = self._fetch_player_trends(player_id)
        baseline_fallback = {} if _has_chart_baselines(row) else self._fetch_chart_baseline_row()
        chart_baselines = _format_chart_baselines(row or {}, baseline_fallback)
        anchor = self._fetch_similarity_anchor(player_id)
        (
            similarity_state,
            similarity_reason,
            similar_players,
        ) = self._get_similar_players(
            player_id,
            anchor=anchor,
        )
        return self._build_player_detail_payload(
            identity=identity,
            row=row,
            archetype_row=anchor,
            similarity_state=similarity_state,
            similarity_reason=similarity_reason,
            similar_players=similar_players,
            game_log=game_log,
            trends=trends,
            chart_baselines=chart_baselines,
        )

    def get_compare(
        self,
        player_a_id: int,
        player_b_id: int,
        *,
        window: CompareWindow = "last_5",
        focus: CompareFocus = "balanced",
    ) -> dict[str, Any]:
        sql = f"""
        SELECT
          season,
          as_of_date,
          player_id,
          player_name,
          latest_team_abbr,
          latest_game_date,
          window_key,
          window_games_expected,
          games_in_window,
          has_full_window,
          avg_min,
          avg_pts,
          avg_reb,
          avg_ast,
          avg_stl,
          avg_blk,
          avg_fg3m,
          avg_tov,
          fantasy_proxy_score
        FROM {self._compare_table()}
        WHERE season = @season
          AND window_key = @window
          AND player_id IN (@player_a_id, @player_b_id)
        """
        rows = self._query(
            sql,
            [
                bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                bigquery.ScalarQueryParameter("window", "STRING", window),
                bigquery.ScalarQueryParameter("player_a_id", "INT64", player_a_id),
                bigquery.ScalarQueryParameter("player_b_id", "INT64", player_b_id),
            ],
        )
        rows_by_player = {int(row["player_id"]): row for row in rows}
        pair_similarity = self._get_pair_similarity(player_a_id, player_b_id)

        def build_side(player_id: int) -> dict[str, Any]:
            row = rows_by_player.get(player_id)
            detail = self.get_player_detail(player_id)
            if detail is None:
                metrics = _empty_compare_metrics()
                return {
                    "player_id": player_id,
                    "player_name": None,
                    "headshot_url": None,
                    "player_initials": "NBA",
                    "latest_team_abbr": None,
                    "latest_game_date": None,
                    "window": window,
                    "window_label": _compare_window_label(window),
                    "state": STATE_UNAVAILABLE,
                    "state_reason": "Player not found",
                    "games_in_window": None,
                    "window_games_expected": _compare_window_expected_games(window),
                    "has_full_window": False,
                    "metrics": metrics,
                    "metric_rows": _build_compare_metric_rows(metrics, focus),
                }

            games_in_window = _to_int((row or {}).get("games_in_window"))
            expected_games = _to_int((row or {}).get("window_games_expected"))
            if expected_games is None:
                expected_games = _compare_window_expected_games(window)
            if row is None:
                state = STATE_UNAVAILABLE
                state_reason = (
                    detail["availability_reason"]
                    if detail["availability_state"] == STATE_UNAVAILABLE
                    else _window_reason(state, _compare_window_label(window))
                )
            else:
                state = _window_state(games_in_window, expected_games)
                state_reason = _window_reason(state, _compare_window_label(window))
            metrics = {
                "fantasy_proxy_score": (row or {}).get("fantasy_proxy_score"),
                "avg_min": (row or {}).get("avg_min"),
                "avg_pts": (row or {}).get("avg_pts"),
                "avg_reb": (row or {}).get("avg_reb"),
                "avg_ast": (row or {}).get("avg_ast"),
                "avg_stl": (row or {}).get("avg_stl"),
                "avg_blk": (row or {}).get("avg_blk"),
                "avg_fg3m": (row or {}).get("avg_fg3m"),
                "avg_tov": (row or {}).get("avg_tov"),
            }
            return {
                "player_id": player_id,
                "player_name": detail["player"]["player_name"],
                "headshot_url": detail["player"].get("headshot_url"),
                "player_initials": detail["player"].get("player_initials"),
                "latest_team_abbr": (row or {}).get(
                    "latest_team_abbr", detail["player"]["team_abbr"]
                ),
                "latest_game_date": (row or {}).get(
                    "latest_game_date", detail["player"]["latest_game_date"]
                ),
                "window": window,
                "window_label": _compare_window_label(window),
                "state": state,
                "state_reason": state_reason,
                "games_in_window": games_in_window,
                "window_games_expected": expected_games,
                "has_full_window": bool((row or {}).get("has_full_window")),
                "availability_state": detail["availability_state"],
                "metrics": metrics,
                "metric_rows": _build_compare_metric_rows(metrics, focus),
                "player_detail": detail,
            }

        return {
            "season": SUPPORTED_SEASON,
            "window": window,
            "window_label": _compare_window_label(window),
            "focus": focus,
            "focus_label": str(COMPARE_FOCUS_CONFIG[focus]["label"]),
            "focus_description": str(COMPARE_FOCUS_CONFIG[focus]["description"]),
            "similarity": pair_similarity,
            "comparison": {
                "player_a": build_side(player_a_id),
                "player_b": build_side(player_b_id),
            },
        }

    def get_latest_analysis(self) -> dict[str, Any] | None:
        table = f"`{self.settings.project_id}.{self.settings.gold_dataset}.analysis_snapshots`"
        sql = f"""
        SELECT
          snapshot_id,
          snapshot_date,
          created_at_utc,
          season,
          headline,
          dek,
          body,
          trend_player,
          trend_stat,
          trend_delta,
          contribution_player_id,
          contribution_player_name,
          contribution_team_abbr,
          contribution_opponent_abbr,
          contribution_matchup,
          contribution_player_pts,
          contribution_team_pts,
          contribution_opponent_team_pts,
          contribution_player_points_share_of_team,
          contribution_player_points_share_of_game,
          contribution_scoring_margin,
          contribution_team_pts_qtr1,
          contribution_team_pts_qtr2,
          contribution_team_pts_qtr3,
          contribution_team_pts_qtr4,
          contribution_team_pts_ot_total,
          contribution_game_date,
          context_player_id,
          context_player_name,
          context_team_abbr,
          context_team_name,
          context_position,
          context_height,
          context_weight,
          context_roster_status,
          context_season_exp,
          context_draft_year,
          context_draft_round,
          context_draft_number,
          freshness_ts,
          source_run_id
        FROM {table}
        WHERE season = @season
        ORDER BY snapshot_date DESC, created_at_utc DESC, snapshot_id DESC
        LIMIT 1
        """
        rows = self._query(
            sql, [bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON)]
        )
        return build_analysis_payload(rows[0]) if rows else None

    def get_latest_successful_run(self) -> dict[str, Any] | None:
        table = f"`{self.settings.project_id}.{self.settings.metadata_dataset}.pipeline_run_log`"
        sql = f"""
        SELECT
          season,
          finished_at_utc
        FROM {table}
        WHERE season = @season
          AND status = 'success'
        ORDER BY finished_at_utc DESC
        LIMIT 1
        """
        rows = self._query(
            sql, [bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON)]
        )
        return rows[0] if rows else None

    def get_player_game_log(
        self,
        player_id: int,
        limit: int = 30,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any] | None:
        identity = self._fetch_player_identity(player_id)
        if identity is None:
            return None
        return self._fetch_player_game_log_payload(
            identity,
            limit=limit,
            start_date=start_date,
            end_date=end_date,
        )

    def get_metric_leaders(self, metric: str, limit: int = 10) -> list[dict[str, Any]]:
        config = _get_agent_metric_leader_config(metric)
        if config is None:
            raise ValueError(f"Unsupported metric for leaderboard: {metric}")
        column = config["column"]
        percentile_column = config["percentile_column"]
        order = config["order"]
        sql = f"""
        SELECT
          season,
          player_id,
          player_name,
          latest_team_abbr AS team_abbr,
          games_sampled,
          sample_status,
          @metric_key AS metric_key,
          @metric_label AS metric_label,
          ROUND({column}, 2) AS metric_value,
          {percentile_column} AS percentile
        FROM {self._category_profile_table()}
        WHERE season = @season
          AND is_qualified
        ORDER BY {column} {order}, games_sampled DESC, player_name
        LIMIT @limit
        """
        try:
            rows = self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                    bigquery.ScalarQueryParameter("metric_key", "STRING", metric),
                    bigquery.ScalarQueryParameter(
                        "metric_label", "STRING", config["label"]
                    ),
                    bigquery.ScalarQueryParameter("limit", "INT64", limit),
                ],
            )
        except BQAPIError:
            legacy_sql = f"""
            SELECT
              season,
              player_id,
              player_name,
              latest_team_abbr AS team_abbr,
              games_sampled,
              CASE
                WHEN games_sampled >= 10 THEN 'ready'
                WHEN games_sampled >= 5 THEN 'limited_sample'
                ELSE 'insufficient_sample'
              END AS sample_status,
              @metric_key AS metric_key,
              @metric_label AS metric_label,
              ROUND({column}, 2) AS metric_value,
              NULL AS percentile
            FROM {self._category_profile_table()}
            WHERE season = @season
              AND games_sampled >= 5
              AND {column} IS NOT NULL
            ORDER BY {column} {order}, games_sampled DESC, player_name
            LIMIT @limit
            """
            try:
                rows = self._query(
                    legacy_sql,
                    [
                        bigquery.ScalarQueryParameter(
                            "season", "STRING", SUPPORTED_SEASON
                        ),
                        bigquery.ScalarQueryParameter("metric_key", "STRING", metric),
                        bigquery.ScalarQueryParameter(
                            "metric_label", "STRING", config["label"]
                        ),
                        bigquery.ScalarQueryParameter("limit", "INT64", limit),
                    ],
                )
            except BQAPIError:
                return []
        return [
            {
                "season": row.get("season"),
                "player_id": _to_int(row.get("player_id")),
                "player_name": row.get("player_name"),
                "team_abbr": row.get("team_abbr"),
                "games_sampled": _to_int(row.get("games_sampled")),
                "sample_status": row.get("sample_status"),
                "metric_key": row.get("metric_key"),
                "metric_label": row.get("metric_label"),
                "metric_value": _to_float(row.get("metric_value")),
                "percentile": _to_float(row.get("percentile")),
            }
            for row in rows
        ]

    def get_player_metric_percentile(
        self, player_id: int, metric: str, min_games: int = 5
    ) -> dict[str, Any] | None:
        config = _get_agent_metric_leader_config(metric)
        if config is None:
            raise ValueError(f"Unsupported metric for percentile: {metric}")
        column = config["column"]
        rank_order = "ASC" if config["order"] == "ASC" else "DESC"
        percentile_order = "DESC" if config["order"] == "ASC" else "ASC"
        min_games = max(1, min(200, int(min_games)))
        sql = f"""
        WITH player_rows AS (
          SELECT
            season,
            player_id,
            player_name,
            latest_team_abbr AS team_abbr,
            games_sampled,
            ROUND({column}, 2) AS metric_value
          FROM {self._category_profile_table()}
          WHERE season = @season
            AND {column} IS NOT NULL
        ),
        cohort AS (
          SELECT *
          FROM player_rows
          WHERE games_sampled >= @min_games
        ),
        ranked AS (
          SELECT
            *,
            RANK() OVER (ORDER BY metric_value {rank_order}, player_name) AS cohort_rank,
            ROUND(
              CASE
                WHEN COUNT(*) OVER () <= 1 THEN 100
                ELSE PERCENT_RANK() OVER (ORDER BY metric_value {percentile_order}) * 100
              END,
              1
            ) AS percentile
          FROM cohort
        ),
        all_summary AS (
          SELECT
            COUNT(*) AS player_count,
            MAX(games_sampled) AS max_games_sampled
          FROM player_rows
        ),
        cohort_summary AS (
          SELECT
            COUNT(*) AS cohort_size,
            ROUND(AVG(metric_value), 2) AS cohort_avg
          FROM cohort
        )
        SELECT
          @metric_key AS metric_key,
          @metric_label AS metric_label,
          @min_games AS min_games,
          p.season,
          p.player_id,
          p.player_name,
          p.team_abbr,
          p.games_sampled,
          p.metric_value,
          r.cohort_rank,
          r.percentile,
          cs.cohort_size,
          cs.cohort_avg,
          s.player_count,
          s.max_games_sampled
        FROM all_summary s
        CROSS JOIN cohort_summary cs
        LEFT JOIN player_rows p
          ON p.player_id = @player_id
        LEFT JOIN ranked r
          ON r.player_id = p.player_id
        LIMIT 1
        """
        try:
            rows = self._query(
                sql,
                [
                    bigquery.ScalarQueryParameter("season", "STRING", SUPPORTED_SEASON),
                    bigquery.ScalarQueryParameter("player_id", "INT64", player_id),
                    bigquery.ScalarQueryParameter("metric_key", "STRING", metric),
                    bigquery.ScalarQueryParameter(
                        "metric_label", "STRING", config["label"]
                    ),
                    bigquery.ScalarQueryParameter("min_games", "INT64", min_games),
                ],
            )
        except BQAPIError:
            return None
        if not rows:
            return None
        row = rows[0]
        if row.get("player_id") is None:
            return None
        return {
            "season": row.get("season"),
            "player_id": _to_int(row.get("player_id")),
            "player_name": row.get("player_name"),
            "team_abbr": row.get("team_abbr"),
            "games_sampled": _to_int(row.get("games_sampled")),
            "metric_key": row.get("metric_key"),
            "metric_label": row.get("metric_label"),
            "metric_value": _to_float(row.get("metric_value")),
            "min_games": _to_int(row.get("min_games")),
            "cohort_rank": _to_int(row.get("cohort_rank")),
            "percentile": _to_float(row.get("percentile")),
            "cohort_size": _to_int(row.get("cohort_size")),
            "cohort_avg": _to_float(row.get("cohort_avg")),
            "player_count": _to_int(row.get("player_count")),
            "max_games_sampled": _to_int(row.get("max_games_sampled")),
            "in_requested_cohort": row.get("cohort_rank") is not None,
        }

    def get_health(self) -> dict[str, Any]:
        latest_run = self.get_latest_successful_run()
        payload = build_freshness_payload(
            latest_run,
            now=datetime.now(tz=UTC),
            freshness_threshold_hours=self.settings.freshness_threshold_hours,
        )
        payload["season"] = SUPPORTED_SEASON
        return payload


def build_analysis_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None

    item = dict(row)
    item["score_contribution"] = {
        "player_id": _to_int(row.get("contribution_player_id")),
        "player_name": row.get("contribution_player_name"),
        "team_abbr": row.get("contribution_team_abbr"),
        "opponent_abbr": row.get("contribution_opponent_abbr"),
        "matchup": row.get("contribution_matchup"),
        "player_pts": _to_int(row.get("contribution_player_pts")),
        "team_pts": _to_int(row.get("contribution_team_pts")),
        "opponent_team_pts": _to_int(row.get("contribution_opponent_team_pts")),
        "player_points_share_of_team": _to_float(
            row.get("contribution_player_points_share_of_team")
        ),
        "player_points_share_of_game": _to_float(
            row.get("contribution_player_points_share_of_game")
        ),
        "scoring_margin": _to_int(row.get("contribution_scoring_margin")),
        "team_pts_qtr1": _to_int(row.get("contribution_team_pts_qtr1")),
        "team_pts_qtr2": _to_int(row.get("contribution_team_pts_qtr2")),
        "team_pts_qtr3": _to_int(row.get("contribution_team_pts_qtr3")),
        "team_pts_qtr4": _to_int(row.get("contribution_team_pts_qtr4")),
        "team_pts_ot_total": _to_int(row.get("contribution_team_pts_ot_total")),
        "game_date": row.get("contribution_game_date"),
    }
    item["player_context"] = {
        "player_id": _to_int(row.get("context_player_id")),
        "player_name": row.get("context_player_name"),
        "team_abbr": row.get("context_team_abbr"),
        "team_name": row.get("context_team_name"),
        "position": row.get("context_position"),
        "height": row.get("context_height"),
        "weight": _to_int(row.get("context_weight")),
        "roster_status": (
            row.get("context_roster_status")
            if row.get("context_roster_status") in (True, False)
            else (
                str(row.get("context_roster_status")).lower() == "true"
                if row.get("context_roster_status") not in (None, "")
                else None
            )
        ),
        "season_exp": _to_int(row.get("context_season_exp")),
        "draft_year": row.get("context_draft_year"),
        "draft_round": row.get("context_draft_round"),
        "draft_number": row.get("context_draft_number"),
    }
    return item
