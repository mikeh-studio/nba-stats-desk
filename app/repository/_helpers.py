from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from math import sqrt
from typing import Any

from app.agent.catalog import load_semantic_catalog
from app.agent.formulas import FormulaError, compile_formula_sql
from app.config import SUPPORTED_SEASON
from app.repository._constants import (
    AGENT_METRIC_COLUMN_MAP,
    AGENT_METRIC_LEADER_CONFIG,
    COMPARE_FOCUS_CONFIG,
    COMPARE_METRIC_LABELS,
    COMPARE_WINDOW_CONFIG,
    FULL_SEASON_TYPES,
    RECENT_PERFORMANCE_DETAIL_HYDRATE_KEYS,
    RECENT_PERFORMANCE_PERCENT_KEYS,
    RECENT_PERFORMANCE_STAT_CONFIG,
    SIMILARITY_FEATURE_COLUMNS,
    SIMILARITY_FEATURE_WEIGHTS,
    SIMILARITY_TRAIT_LABELS,
    STAT_PERCENTILE_CONFIG,
    STATE_FRESH,
    STATE_INSUFFICIENT_SAMPLE,
    STATE_MISSING,
    STATE_STALE,
    STATE_UNAVAILABLE,
    CompareFocus,
    CompareWindow,
)

_BIG_ARCHETYPE_BASE_LABELS = {"Stretch Big", "Interior Big"}


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


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _archetype_base_label(label: Any) -> str:
    text = str(label or "").split(" - ", 1)[0].strip()
    return text or "Unclassified"


def _position_tokens(position: Any) -> set[str]:
    if position in (None, ""):
        return set()
    tokens = re.split(r"[^a-z0-9]+", str(position).lower())
    resolved: set[str] = set()
    for token in tokens:
        if token in {"g", "pg", "sg", "guard", "guards"}:
            resolved.add("G")
        elif token in {"f", "sf", "pf", "forward", "forwards"}:
            resolved.add("F")
        elif token in {"c", "center", "centre", "centers", "centres"}:
            resolved.add("C")
    return resolved


def _is_guard_only_position(position_tokens: set[str]) -> bool:
    return "G" in position_tokens and not ({"F", "C"} & position_tokens)


def _meets_physical_profile(
    source: dict[str, Any],
    *,
    height_inches: float,
    weight_lbs: float,
    wingspan_inches: float,
    required_hits: int = 2,
) -> bool:
    checks = []
    height = _optional_float(source.get("height_inches"))
    weight = _optional_float(source.get("weight_lbs"))
    wingspan = _optional_float(source.get("wingspan_inches"))
    if height is not None:
        checks.append(height >= height_inches)
    if weight is not None:
        checks.append(weight >= weight_lbs)
    if wingspan is not None:
        checks.append(wingspan >= wingspan_inches)
    if not checks:
        return False
    return sum(checks) >= min(required_hits, len(checks))


def _has_big_physical_profile(source: dict[str, Any]) -> bool:
    height = _optional_float(source.get("height_inches"))
    wingspan = _optional_float(source.get("wingspan_inches"))
    return (
        _meets_physical_profile(
            source,
            height_inches=80.0,
            weight_lbs=230.0,
            wingspan_inches=84.0,
        )
        or (height is not None and height >= 82.0)
        or (wingspan is not None and wingspan >= 86.0)
    )


def _has_forward_physical_profile(source: dict[str, Any]) -> bool:
    height = _optional_float(source.get("height_inches"))
    return _meets_physical_profile(
        source,
        height_inches=78.0,
        weight_lbs=215.0,
        wingspan_inches=81.0,
    ) or (height is not None and height >= 80.0)


def _has_large_guard_physical_profile(source: dict[str, Any]) -> bool:
    return _meets_physical_profile(
        source,
        height_inches=77.0,
        weight_lbs=210.0,
        wingspan_inches=80.0,
        required_hits=1,
    )


def _served_big_role_eligible(source: dict[str, Any]) -> bool:
    position_tokens = _position_tokens(source.get("position"))
    if _is_guard_only_position(position_tokens):
        return False
    if "C" in position_tokens:
        return True
    if "F" in position_tokens:
        return _has_big_physical_profile(source)
    return _has_big_physical_profile(source)


def _source_feature_value(source: dict[str, Any], feature_name: str) -> float:
    for column_name in (f"norm_{feature_name}", feature_name):
        value = _optional_float(source.get(column_name))
        if value is not None:
            return value
    return 0.0


def _feature_signal(source: dict[str, Any], *feature_names: str) -> float:
    values = [
        _source_feature_value(source, feature_name) for feature_name in feature_names
    ]
    return max(values) if values else 0.0


def _role_signal_scores(source: dict[str, Any]) -> dict[str, float]:
    return {
        "scoring": _feature_signal(
            source,
            "season_avg_pts",
            "season_avg_fga",
            "recent_pts",
            "recent_points_share_of_team",
            "recent_points_share_of_game",
            "team_points_contribution_rate",
            "team_fga_contribution_rate",
            "second_half_pts_delta",
        ),
        "creation": _feature_signal(
            source,
            "season_avg_ast",
            "recent_ast",
            "season_ast_to_tov",
            "team_ast_contribution_rate",
        ),
        "spacing": _feature_signal(
            source,
            "season_avg_fg3m",
            "recent_fg3m",
            "season_fg3a_rate",
            "shot_corner3_rate",
            "shot_above_break3_rate",
            "shot_corner3_fg_pct",
            "second_half_ts_delta",
        ),
        "defense": _feature_signal(
            source,
            "season_avg_stl",
            "recent_stl",
            "team_stl_contribution_rate",
            "team_defense_contribution_rate",
        ),
        "interior": _feature_signal(
            source,
            "season_avg_reb",
            "season_avg_blk",
            "recent_reb",
            "recent_blk",
            "team_reb_contribution_rate",
            "team_blk_contribution_rate",
            "height_inches",
            "weight_lbs",
            "wingspan_inches",
        ),
        "offense_context": _feature_signal(
            source,
            "team_offense_contribution_rate",
            "team_points_contribution_rate",
            "team_fga_contribution_rate",
            "team_ast_contribution_rate",
        ),
    }


def _fallback_non_big_label(scores: dict[str, float]) -> str:
    scoring = scores["scoring"]
    creation = scores["creation"]
    spacing = scores["spacing"]
    defense = scores["defense"]
    interior = scores["interior"]
    offense_context = scores["offense_context"]

    if scoring >= 0.7 and creation < 0.9:
        return "Scoring Guard"
    if defense >= 0.2 and spacing >= 0.1:
        return "Two-Way Wing"
    if scoring >= 0.45 and creation < 0.55:
        return "Role Scorer"
    if creation >= 0.35 and offense_context >= 0.10:
        return "Secondary Creator"
    if spacing >= 0.25:
        return "Spacing Wing"
    if defense >= 0.25:
        return "Defensive Specialist"
    if interior >= 0.25:
        return "Utility Forward"
    if scoring >= 0.25:
        return "Role Scorer"

    fallback_options = [
        ("spacing", "Spacing Wing"),
        ("creation", "Secondary Creator"),
        ("defense", "Defensive Specialist"),
        ("interior", "Utility Forward"),
        ("scoring", "Role Scorer"),
    ]
    signal_name, label = max(fallback_options, key=lambda item: scores[item[0]])
    if scores[signal_name] >= 0.10:
        return label
    return "Connector Wing"


def _position_guarded_non_big_label(source: dict[str, Any]) -> str:
    scores = _role_signal_scores(source)
    position_tokens = _position_tokens(source.get("position"))

    if _is_guard_only_position(position_tokens):
        if scores["creation"] >= 0.35 and scores["offense_context"] >= 0.10:
            return "Secondary Creator"
        if scores["scoring"] >= 0.45 and scores["creation"] < 0.55:
            return "Scoring Guard"
        if scores["spacing"] >= 0.25:
            return "Spacing Guard"
        if _has_large_guard_physical_profile(source):
            return "Big Guard"

    if "F" in position_tokens and "C" not in position_tokens:
        if scores["spacing"] >= 0.25 and _has_forward_physical_profile(source):
            return "Stretch Forward"
        if scores["interior"] >= 0.25 and _has_forward_physical_profile(source):
            return "Interior Forward"

    return _fallback_non_big_label(scores)


def _guard_served_archetype_label(
    label: Any, source: dict[str, Any] | None
) -> str | None:
    if label in (None, ""):
        return None
    text = str(label)
    if source is None:
        return text
    if _archetype_base_label(text) not in _BIG_ARCHETYPE_BASE_LABELS:
        return text
    if _served_big_role_eligible(source):
        return text

    replacement = _position_guarded_non_big_label(source)
    parts = text.split(" - ", 1)
    if len(parts) == 2 and parts[1].strip():
        return f"{replacement} - {parts[1].strip()}"
    return replacement


def _guard_served_archetype_summary(
    summary: Any,
    *,
    original_label: Any,
    guarded_label: Any,
) -> Any:
    if not isinstance(summary, str) or not summary:
        return summary
    if (
        not original_label
        or not guarded_label
        or str(original_label) == str(guarded_label)
    ):
        return summary
    replacements = (
        (str(original_label), str(guarded_label)),
        (_archetype_base_label(original_label), _archetype_base_label(guarded_label)),
    )
    for original, guarded in replacements:
        if original and summary.startswith(original):
            return f"{guarded}{summary[len(original) :]}"
    return summary


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
        value = value.strip()
        if not value:
            return None
        candidates = [value]
        if len(value) >= 10:
            candidates.append(value[:10])
        for candidate in candidates:
            try:
                return date.fromisoformat(candidate)
            except ValueError:
                pass
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _format_iso_date(value: Any) -> str | None:
    parsed = _parse_iso_date(value)
    if parsed is not None:
        return parsed.isoformat()
    return _to_iso(value)


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
            "expected_games": config["expected_games"],
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


def _compare_window_expected_games(window: CompareWindow) -> int | None:
    value = COMPARE_WINDOW_CONFIG[window]["expected_games"]
    return int(value) if value is not None else None


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
        f"https://cdn.nba.com/headshots/nba/latest/1040x760/{normalized_player_id}.png"
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


def build_season_coverage_payload(
    row: dict[str, Any] | None,
    *,
    season: str = SUPPORTED_SEASON,
) -> dict[str, Any] | None:
    if row is None:
        return None

    game_count = _to_int(row.get("game_count"))
    if not game_count:
        return None

    season_types = [
        label
        for label, field_name in (
            ("Regular Season", "has_regular_season"),
            ("Playoffs", "has_playoffs"),
        )
        if _to_bool(row.get(field_name)) is True
    ]
    is_full_season = all(label in season_types for label in FULL_SEASON_TYPES)
    return {
        "season": row.get("season") or season,
        "first_game_date": row.get("first_game_date"),
        "latest_game_date": row.get("latest_game_date"),
        "game_count": game_count,
        "player_game_rows": _to_int(row.get("player_game_rows")),
        "season_types": season_types,
        "is_full_season": is_full_season,
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
    return any(
        row.get(config["baseline_field"]) is not None
        for config in STAT_PERCENTILE_CONFIG
    )


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
        "fg3a",
        "fgm",
        "fga",
        "ftm",
        "fta",
        "plus_minus",
    ):
        parsed = _to_int(item.get(key))
        if parsed is not None:
            item[key] = parsed
    for key in ("min", "fg_pct", "ft_pct", "fantasy_points_simple"):
        parsed_float = _to_float(item.get(key))
        if parsed_float is not None:
            item[key] = round(parsed_float, 3 if key.endswith("_pct") else 1)
    return item


def _round_float(value: Any, digits: int = 1) -> float | None:
    parsed = _to_float(value)
    if parsed is None:
        return None
    return round(parsed, digits)


def _recent_performance_status(
    score: float | None, above_count: int | None, below_count: int | None
) -> str:
    if score is None:
        return STATE_UNAVAILABLE
    if score >= 1.0 or (score > 0 and (above_count or 0) >= 3):
        return "above"
    if score <= -1.0 or (score < 0 and (below_count or 0) >= 3):
        return "below"
    return "near"


def _format_recent_performance_metric_value(key: str, value: Any) -> float | None:
    parsed = _to_float(value)
    if parsed is None:
        return None
    if key in RECENT_PERFORMANCE_PERCENT_KEYS:
        return round(parsed * 100, 1)
    return round(parsed, 1)


def _format_recent_performance_metric(
    row: dict[str, Any],
    *,
    key: str,
    label: str,
    include_range: bool,
    value_format: str | None = None,
) -> dict[str, Any]:
    value = _format_recent_performance_metric_value(key, row.get(key))
    average = _format_recent_performance_metric_value(key, row.get(f"avg_{key}"))
    delta = _format_recent_performance_metric_value(key, row.get(f"{key}_delta"))
    metric_status = "near"
    if delta is not None:
        if delta > 0:
            metric_status = "above"
        elif delta < 0:
            metric_status = "below"
    payload: dict[str, Any] = {
        "key": key,
        "label": label,
        "value": value,
        "season_average": average,
        "delta": delta,
        "delta_pct": _round_float(row.get(f"{key}_delta_pct"), 1),
        "status": metric_status,
        "format": value_format,
    }
    if include_range:
        percentile = _round_float(row.get(f"{key}_percentile"), 1)
        payload["percentile"] = _clamp_percentile(percentile)
        payload["range"] = {
            "p10": _format_recent_performance_metric_value(key, row.get(f"{key}_p10")),
            "p25": _format_recent_performance_metric_value(key, row.get(f"{key}_p25")),
            "median": _format_recent_performance_metric_value(
                key, row.get(f"{key}_p50")
            ),
            "p75": _format_recent_performance_metric_value(key, row.get(f"{key}_p75")),
            "p90": _format_recent_performance_metric_value(key, row.get(f"{key}_p90")),
        }
    return payload


def _format_recent_performance_row(
    row: dict[str, Any], *, include_range: bool = False
) -> dict[str, Any]:
    score = _round_float(row.get("performance_score"), 2)
    above_count = _to_int(row.get("above_count"))
    below_count = _to_int(row.get("below_count"))
    status = row.get("performance_status")
    if status not in ("above", "below", "near"):
        status = _recent_performance_status(score, above_count, below_count)
    return {
        "game_id": row.get("game_id"),
        "game_date": _format_iso_date(row.get("game_date")),
        "season_type": row.get("season_type"),
        "player_id": _to_int(row.get("player_id")),
        "player_name": row.get("player_name"),
        "team_abbr": row.get("team_abbr"),
        "opponent_abbr": row.get("opponent_abbr"),
        "home_away": row.get("home_away"),
        "matchup": row.get("matchup"),
        "wl": row.get("wl"),
        "minutes": _round_float(row.get("min"), 1),
        "games_sampled": _to_int(row.get("games_sampled")),
        "performance_score": score,
        "performance_status": status,
        "above_count": above_count,
        "below_count": below_count,
        "headshot_url": build_headshot_url(row.get("player_id")),
        "player_initials": build_player_initials(row.get("player_name")),
        "metrics": [
            _format_recent_performance_metric(
                row,
                key=config["key"],
                label=config["label"],
                include_range=include_range,
                value_format=config.get("format"),
            )
            for config in RECENT_PERFORMANCE_STAT_CONFIG
        ],
    }


def _format_recent_performance_trend(rows: list[dict[str, Any]]) -> dict[str, Any]:
    first_row = rows[0] if rows else {}
    return {
        "window_days": 30,
        "stats": [
            {
                "key": config["key"],
                "label": config["label"],
                "season_average": _format_recent_performance_metric_value(
                    config["key"], first_row.get(f"avg_{config['key']}")
                ),
                "format": config.get("format"),
            }
            for config in RECENT_PERFORMANCE_STAT_CONFIG
        ],
        "points": [
            (
                {
                    "game_id": row.get("game_id"),
                    "game_date": _format_iso_date(row.get("game_date")),
                    "matchup": row.get("matchup"),
                    "minutes": _round_float(row.get("min"), 1),
                }
                | {
                    config["key"]: _format_recent_performance_metric_value(
                        config["key"], row.get(config["key"])
                    )
                    for config in RECENT_PERFORMANCE_STAT_CONFIG
                }
            )
            for row in rows
        ],
    }


def _recent_performance_metric_map(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    metrics = item.get("metrics")
    if not isinstance(metrics, list):
        return {}
    return {
        str(metric.get("key")): metric
        for metric in metrics
        if isinstance(metric, dict) and metric.get("key") is not None
    }


def _recent_performance_detail_needs_hydration(item: dict[str, Any]) -> bool:
    metrics = _recent_performance_metric_map(item)
    for key in RECENT_PERFORMANCE_DETAIL_HYDRATE_KEYS:
        metric = metrics.get(key)
        if metric is None:
            return True
        if key == "min":
            if metric.get("season_average") is None or metric.get("delta") is None:
                return True
            continue
        if (
            metric.get("value") is None
            or metric.get("season_average") is None
            or metric.get("delta") is None
        ):
            return True
    return False


def _recent_performance_trend_needs_hydration(item: dict[str, Any]) -> bool:
    trend = item.get("trend_30d")
    if not isinstance(trend, dict):
        return True
    points = trend.get("points")
    if not isinstance(points, list) or not points:
        return True
    for point in points:
        if not isinstance(point, dict):
            return True
        if any(point.get(key) is None for key in ("fg_pct", "ft_pct", "fg3m")):
            return True
    return False


def _merge_recent_performance_detail_summary(
    item: dict[str, Any], source: dict[str, Any]
) -> dict[str, Any]:
    for field_name in (
        "season_type",
        "minutes",
        "games_sampled",
        "performance_score",
        "performance_status",
        "above_count",
        "below_count",
    ):
        source_value = source.get(field_name)
        if source_value is not None:
            item[field_name] = source_value

    target_metrics = _recent_performance_metric_map(item)
    source_metrics = _recent_performance_metric_map(source)
    for config in RECENT_PERFORMANCE_STAT_CONFIG:
        key = config["key"]
        source_metric = source_metrics.get(key)
        if source_metric is None:
            continue
        target_metric = target_metrics.get(key)
        if target_metric is None:
            item.setdefault("metrics", []).append(dict(source_metric))
            continue
        for field_name in ("value", "season_average", "delta", "delta_pct", "status"):
            source_value = source_metric.get(field_name)
            if source_value is not None:
                target_metric[field_name] = source_value
        target_metric["format"] = source_metric.get("format")
    return item


def _format_recent_performance_game(row: dict[str, Any]) -> dict[str, Any]:
    away_team = row.get("away_team_abbr")
    home_team = row.get("home_team_abbr")
    matchup: str | None
    if away_team and home_team:
        matchup = f"{away_team} @ {home_team}"
    else:
        matchup = row.get("matchup") or row.get("teams")
    return {
        "game_id": row.get("game_id"),
        "game_date": row.get("game_date"),
        "matchup": matchup,
        "teams": row.get("teams"),
        "home_team_abbr": home_team,
        "away_team_abbr": away_team,
        "home_team_pts": _to_int(row.get("home_team_pts")),
        "away_team_pts": _to_int(row.get("away_team_pts")),
        "players_played": _to_int(row.get("players_played")),
    }


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
