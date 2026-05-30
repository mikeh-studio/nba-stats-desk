from __future__ import annotations

from datetime import date
from typing import Any

from app.agent.catalog import (
    DEFAULT_METRIC_KEYS,
    MetricDefinition,
    SemanticCatalog,
    load_semantic_catalog,
)
from app.agent.formulas import evaluate_formula
from app.repository import WarehouseRepository


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _coerce_limit(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    coerced = _to_int(value)
    if coerced is None:
        return default
    return max(minimum, min(maximum, coerced))


def _coerce_date(value: Any, *, field_name: str) -> tuple[str | None, str | None]:
    if value in (None, ""):
        return None, None
    text = str(value).strip()
    try:
        return date.fromisoformat(text).isoformat(), None
    except ValueError:
        return None, f"{field_name} must be an ISO date in YYYY-MM-DD format."


def _coerce_date_range(
    start_date: Any,
    end_date: Any,
) -> tuple[dict[str, str | None], str | None]:
    start, start_error = _coerce_date(start_date, field_name="start_date")
    if start_error:
        return {"start_date": None, "end_date": None}, start_error
    end, end_error = _coerce_date(end_date, field_name="end_date")
    if end_error:
        return {"start_date": start, "end_date": None}, end_error
    if start and end and start > end:
        return (
            {"start_date": start, "end_date": end},
            "start_date must be on or before end_date.",
        )
    return {"start_date": start, "end_date": end}, None


def _compact_player(player: dict[str, Any]) -> dict[str, Any]:
    return {
        "player_id": _to_int(player.get("player_id")),
        "player_name": player.get("player_name"),
        "team_abbr": player.get("team_abbr") or player.get("latest_team_abbr"),
        "latest_game_date": player.get("latest_game_date"),
        "games_sampled": _to_int(player.get("games_sampled")),
        "sample_status": player.get("sample_status"),
        "overall_rank": _to_int(player.get("overall_rank")),
        "recommendation_score": _to_float(player.get("recommendation_score")),
    }


def _metric_values(
    row: dict[str, Any],
    metrics: list[MetricDefinition],
) -> dict[str, float | None]:
    return {metric.key: _game_metric_value(row, metric) for metric in metrics}


def _game_metric_value(row: dict[str, Any], metric: MetricDefinition) -> float | None:
    if metric.formula:
        value = evaluate_formula(metric.formula, row)
        return round(value, 2) if value is not None else None
    return _to_float(row.get(metric.game_log_key))


def _game_metric_meta(row: dict[str, Any], metric: MetricDefinition) -> str:
    meta_parts = [
        str(row.get("matchup") or "").strip(),
        str(row.get("wl") or "").strip(),
    ]
    if metric.formula:
        inputs = []
        for variable in metric.formula_variables:
            value = _to_float(row.get(variable))
            if value is not None:
                inputs.append(f"{variable.upper()} {value:g}")
        if inputs:
            meta_parts.append(" · ".join(inputs))
        meta_parts.append(f"formula {metric.formula.upper()}")
    return " ".join(part for part in meta_parts if part)


def _average(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def _build_game_log_trend_row(
    games: list[dict[str, Any]],
    metric: MetricDefinition,
) -> dict[str, Any] | None:
    values: list[float] = []
    for game in games:
        value = _game_metric_value(game, metric)
        if value is not None:
            values.append(value)
    if not values:
        return None
    recent = values[-5:]
    prior = values[-10:-5] if len(values) > 5 else []
    recent_avg = _average(recent)
    prior_avg = _average(prior)
    delta = (
        round(recent_avg - prior_avg, 1)
        if recent_avg is not None and prior_avg is not None and prior_avg != 0
        else None
    )
    pct_change = (
        round((delta / prior_avg) * 100, 1)
        if delta is not None and prior_avg is not None and prior_avg != 0
        else None
    )
    return {
        "stat": metric.trend_stat,
        "label": metric.label,
        "formula": metric.formula.upper() if metric.formula else None,
        "recent_games": len(recent),
        "prior_games": len(prior),
        "recent_avg": recent_avg,
        "prior_avg": prior_avg,
        "delta": delta,
        "pct_change": pct_change,
        "direction_is_good": metric.higher_is_better,
    }


def _build_line_chart(
    *,
    title: str,
    games: list[dict[str, Any]],
    metrics: list[MetricDefinition],
) -> dict[str, Any]:
    series = []
    for metric in metrics:
        points = []
        for game in games:
            value = _game_metric_value(game, metric)
            if value is None:
                continue
            points.append(
                {
                    "x": str(game.get("game_date") or ""),
                    "y": value,
                    "meta": _game_metric_meta(game, metric),
                }
            )
        if points:
            series.append({"key": metric.key, "label": metric.label, "points": points})
    return {
        "type": "line",
        "title": title,
        "x_label": "Game date",
        "y_label": "Stat value",
        "series": series,
    }


def _build_percentile_chart(
    player_name: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    points = [
        {
            "x": str(row.get("label") or row.get("key") or ""),
            "y": _to_float(row.get("percentile")) or 0.0,
            "meta": f"{row.get('average')} avg",
        }
        for row in rows
    ]
    return {
        "type": "bar",
        "title": f"{player_name} league percentiles",
        "x_label": "Metric",
        "y_label": "Percentile",
        "series": [{"key": "percentile", "label": "Percentile", "points": points}],
    }


def get_tool_schemas() -> list[dict[str, Any]]:
    metric_array = {
        "type": ["array", "null"],
        "items": {"type": "string"},
        "description": "Metric keys or aliases such as pts, rebounds, assists, blocks, turnovers.",
    }
    date_filter = {
        "type": ["string", "null"],
        "description": "Optional inclusive game_date filter in YYYY-MM-DD format.",
    }
    return [
        {
            "type": "function",
            "name": "list_metrics",
            "description": "List approved semantic metrics the NBA stats agent can use.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": ["string", "null"],
                        "description": "Optional user wording to match against metric names.",
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "resolve_player",
            "description": "Resolve a player name to qualified 2025-26 player matches.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Player name query."},
                    "limit": {
                        "type": "integer",
                        "description": "Maximum matches, 1-8.",
                    },
                },
                "required": ["name", "limit"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "get_player_summary",
            "description": "Get a compact player summary from gold player detail data.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "player_id": {"type": "integer", "description": "NBA player id."}
                },
                "required": ["player_id"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "get_player_game_log",
            "description": "Get recent game log rows and line-chart data for approved metrics.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "player_id": {"type": "integer", "description": "NBA player id."},
                    "metrics": metric_array,
                    "limit": {"type": "integer", "description": "Game count, 1-82."},
                    "start_date": date_filter,
                    "end_date": date_filter,
                },
                "required": [
                    "player_id",
                    "metrics",
                    "limit",
                    "start_date",
                    "end_date",
                ],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "get_player_trends",
            "description": "Get recent-vs-prior trends and chart-ready game data for a player.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "player_id": {"type": "integer", "description": "NBA player id."},
                    "metrics": metric_array,
                    "start_date": date_filter,
                    "end_date": date_filter,
                },
                "required": ["player_id", "metrics", "start_date", "end_date"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "get_player_percentiles",
            "description": "Get league percentile bars for the main player stats.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "player_id": {"type": "integer", "description": "NBA player id."},
                    "metrics": metric_array,
                },
                "required": ["player_id", "metrics"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "calculate_player_percentile",
            "description": (
                "Calculate one player's percentile for an approved metric within a "
                "minimum-games cohort. Use this for custom percentile questions, "
                "including Attributed Points / points created = PTS + AST * 2."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "player_id": {"type": "integer", "description": "NBA player id."},
                    "metric": {
                        "type": "string",
                        "description": "Metric key or alias. Use points_created for PTS + AST * 2.",
                    },
                    "min_games": {
                        "type": "integer",
                        "description": "Minimum games in the comparison cohort.",
                    },
                },
                "required": ["player_id", "metric", "min_games"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "find_similar_players",
            "description": "Return similar players from the Spark/ML similarity outputs.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "player_id": {"type": "integer", "description": "NBA player id."},
                    "limit": {
                        "type": "integer",
                        "description": "Maximum matches, 1-6.",
                    },
                },
                "required": ["player_id", "limit"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "search_rankings",
            "description": "Rank qualified players by an approved semantic metric.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "description": "Metric key or alias."},
                    "limit": {"type": "integer", "description": "Maximum rows, 1-25."},
                },
                "required": ["metric", "limit"],
                "additionalProperties": False,
            },
        },
    ]


class StatsToolRunner:
    def __init__(
        self,
        repo: WarehouseRepository,
        catalog: SemanticCatalog | None = None,
    ) -> None:
        self.repo = repo
        self.catalog = catalog or load_semantic_catalog()

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "list_metrics":
            return self.list_metrics(args.get("query"))
        if name == "resolve_player":
            return self.resolve_player(args.get("name"), args.get("limit"))
        if name == "get_player_summary":
            return self.get_player_summary(args.get("player_id"))
        if name == "get_player_game_log":
            return self.get_player_game_log(
                args.get("player_id"),
                args.get("metrics"),
                args.get("limit"),
                args.get("start_date"),
                args.get("end_date"),
            )
        if name == "get_player_trends":
            return self.get_player_trends(
                args.get("player_id"),
                args.get("metrics"),
                args.get("start_date"),
                args.get("end_date"),
            )
        if name == "get_player_percentiles":
            return self.get_player_percentiles(
                args.get("player_id"),
                args.get("metrics"),
            )
        if name == "calculate_player_percentile":
            return self.calculate_player_percentile(
                args.get("player_id"),
                args.get("metric"),
                args.get("min_games"),
            )
        if name == "find_similar_players":
            return self.find_similar_players(args.get("player_id"), args.get("limit"))
        if name == "search_rankings":
            return self.search_rankings(args.get("metric"), args.get("limit"))
        return {"status": "error", "message": f"Unknown tool: {name}"}

    def list_metrics(self, query: str | None = None) -> dict[str, Any]:
        metrics = self.catalog.list_metrics()
        if query:
            query_norm = str(query).lower()
            metrics = [
                metric
                for metric in metrics
                if query_norm in metric["key"].lower()
                or query_norm in metric["label"].lower()
                or any(query_norm in alias.lower() for alias in metric["aliases"])
            ]
        return {"status": "ok", "metrics": metrics}

    def resolve_player(self, name: Any, limit: Any = 5) -> dict[str, Any]:
        query = str(name or "").strip()
        if not query:
            return {"status": "error", "message": "Player name is required."}
        rows = self.repo.search_players(
            query,
            limit=_coerce_limit(limit, default=5, minimum=1, maximum=8),
        )
        matches = [_compact_player(row) for row in rows]
        if not matches:
            return {"status": "not_found", "query": query, "matches": []}
        exact = [
            match
            for match in matches
            if str(match.get("player_name") or "").lower() == query.lower()
        ]
        if len(matches) == 1 or len(exact) == 1:
            return {
                "status": "ok",
                "query": query,
                "player": exact[0] if exact else matches[0],
                "matches": matches,
            }
        return {"status": "ambiguous", "query": query, "matches": matches}

    def get_player_summary(self, player_id: Any) -> dict[str, Any]:
        detail = self.repo.get_player_detail(_to_int(player_id) or -1)
        if detail is None:
            return {"status": "not_found", "player_id": player_id}
        player = detail.get("player") or {}
        return {
            "status": "ok",
            "source_models": [
                "agent.agent_player_search",
                "gold.workbench_player_detail",
                "gold.player_category_profile",
                "gold.player_similarity_features",
            ],
            "player": _compact_player(player),
            "sample": detail.get("sample") or {},
            "availability_state": detail.get("availability_state"),
            "availability_reason": detail.get("availability_reason"),
            "reason_summary": detail.get("reason_summary"),
            "trend": detail.get("trend") or {},
            "recent_form": detail.get("recent_form") or [],
            "stat_percentiles": detail.get("stat_percentiles") or [],
            "league_baselines": detail.get("chart_baselines") or {},
            "archetype": detail.get("archetype") or {},
            "similar_players": (detail.get("similar_players") or [])[:6],
        }

    def get_player_game_log(
        self,
        player_id: Any,
        metrics: list[str] | None,
        limit: Any = 10,
        start_date: Any = None,
        end_date: Any = None,
    ) -> dict[str, Any]:
        selected, invalid = self.catalog.resolve_metrics(
            metrics,
            default_keys=DEFAULT_METRIC_KEYS,
        )
        if invalid:
            return {
                "status": "error",
                "message": "Unsupported metric.",
                "invalid_metrics": invalid,
                "valid_metrics": self.catalog.list_metrics(),
            }
        date_range, date_error = _coerce_date_range(start_date, end_date)
        if date_error:
            return {
                "status": "error",
                "message": date_error,
                "date_range": date_range,
            }
        player_id_int = _to_int(player_id) or -1
        game_limit = _coerce_limit(limit, default=10, minimum=1, maximum=82)
        log = self.repo.get_player_game_log(
            player_id_int,
            limit=game_limit,
            start_date=date_range["start_date"],
            end_date=date_range["end_date"],
        )
        if log is None:
            return {"status": "not_found", "player_id": player_id}
        games = list(log.get("games") or [])
        rows = [
            {
                "game_date": game.get("game_date"),
                "matchup": game.get("matchup"),
                "team_abbr": game.get("team_abbr"),
                "opponent_abbr": game.get("opponent_abbr"),
                "wl": game.get("wl"),
                "metrics": _metric_values(game, selected),
            }
            for game in games
        ]
        return {
            "status": "ok",
            "player_id": player_id_int,
            "player_name": log.get("player_name"),
            "season": log.get("season"),
            "metrics": [metric.to_public_dict() for metric in selected],
            "date_range": date_range,
            "games_returned": len(rows),
            "rows": rows,
            "charts": [
                _build_line_chart(
                    title=f"{log.get('player_name')} game log",
                    games=games,
                    metrics=selected,
                )
            ],
        }

    def get_player_trends(
        self,
        player_id: Any,
        metrics: list[str] | None,
        start_date: Any = None,
        end_date: Any = None,
    ) -> dict[str, Any]:
        selected, invalid = self.catalog.resolve_metrics(
            metrics,
            default_keys=DEFAULT_METRIC_KEYS,
        )
        if invalid:
            return {
                "status": "error",
                "message": "Unsupported metric.",
                "invalid_metrics": invalid,
                "valid_metrics": self.catalog.list_metrics(),
            }
        date_range, date_error = _coerce_date_range(start_date, end_date)
        if date_error:
            return {
                "status": "error",
                "message": date_error,
                "date_range": date_range,
            }
        player_id_int = _to_int(player_id) or -1
        detail = self.repo.get_player_detail(player_id_int)
        if detail is None:
            return {"status": "not_found", "player_id": player_id}
        has_date_filter = bool(date_range["start_date"] or date_range["end_date"])
        if has_date_filter:
            game_log = (
                self.repo.get_player_game_log(
                    player_id_int,
                    limit=82,
                    start_date=date_range["start_date"],
                    end_date=date_range["end_date"],
                )
                or {}
            )
            trends = []
        else:
            selected_stats = {metric.trend_stat for metric in selected}
            trends = [
                row
                for row in (detail.get("trends") or [])
                if row.get("stat") in selected_stats
                or row.get("label") in selected_stats
            ]
            game_log = detail.get("game_log") or {}
        games = list(game_log.get("games") or [])
        existing_stats = {row.get("stat") for row in trends}
        for metric in selected:
            if metric.trend_stat in existing_stats:
                continue
            derived_trend = _build_game_log_trend_row(games, metric)
            if derived_trend is not None:
                trends.append(derived_trend)
        player = detail.get("player") or {}
        return {
            "status": "ok",
            "player": _compact_player(player),
            "date_range": date_range,
            "trends": trends,
            "charts": [
                _build_line_chart(
                    title=f"{player.get('player_name')} trend",
                    games=games,
                    metrics=selected,
                )
            ],
            "league_baselines": detail.get("chart_baselines") or {},
        }

    def get_player_percentiles(
        self,
        player_id: Any,
        metrics: list[str] | None,
    ) -> dict[str, Any]:
        selected, invalid = self.catalog.resolve_metrics(
            metrics,
            default_keys=DEFAULT_METRIC_KEYS,
        )
        if invalid:
            return {
                "status": "error",
                "message": "Unsupported metric.",
                "invalid_metrics": invalid,
                "valid_metrics": self.catalog.list_metrics(),
            }
        detail = self.repo.get_player_detail(_to_int(player_id) or -1)
        if detail is None:
            return {"status": "not_found", "player_id": player_id}
        selected_keys = {metric.key for metric in selected}
        rows = [
            row
            for row in (detail.get("stat_percentiles") or [])
            if row.get("key") in selected_keys
        ]
        player = detail.get("player") or {}
        return {
            "status": "ok",
            "player": _compact_player(player),
            "percentiles": rows,
            "charts": [
                _build_percentile_chart(player.get("player_name", "Player"), rows)
            ],
        }

    def calculate_player_percentile(
        self,
        player_id: Any,
        metric: Any,
        min_games: Any = 5,
    ) -> dict[str, Any]:
        metric_def = self.catalog.resolve_metric(str(metric or ""))
        if metric_def is None:
            return {
                "status": "error",
                "message": "Unsupported metric.",
                "invalid_metrics": [metric],
                "valid_metrics": self.catalog.list_metrics(),
            }
        min_games_int = _coerce_limit(min_games, default=5, minimum=1, maximum=200)
        result = self.repo.get_player_metric_percentile(
            _to_int(player_id) or -1,
            metric_def.key,
            min_games=min_games_int,
        )
        if result is None:
            return {"status": "not_found", "player_id": player_id}
        result["status"] = "ok"
        result["metric"] = metric_def.to_public_dict()
        if not result.get("in_requested_cohort"):
            result["message"] = (
                f"{result.get('player_name')} has {result.get('games_sampled')} games, "
                f"so they are not in the requested {min_games_int}+ game cohort."
            )
            if not result.get("cohort_size"):
                result["message"] = (
                    f"No players are in the requested {min_games_int}+ game cohort. "
                    f"The maximum games_sampled in this table is {result.get('max_games_sampled')}."
                )
        return result

    def find_similar_players(self, player_id: Any, limit: Any = 5) -> dict[str, Any]:
        detail = self.repo.get_player_detail(_to_int(player_id) or -1)
        if detail is None:
            return {"status": "not_found", "player_id": player_id}
        max_rows = _coerce_limit(limit, default=5, minimum=1, maximum=6)
        return {
            "status": "ok",
            "player": _compact_player(detail.get("player") or {}),
            "similarity_reason": detail.get("similarity_reason"),
            "similar_players": (detail.get("similar_players") or [])[:max_rows],
        }

    def search_rankings(self, metric: Any, limit: Any = 10) -> dict[str, Any]:
        metric_def = self.catalog.resolve_metric(str(metric or ""))
        if metric_def is None:
            return {
                "status": "error",
                "message": "Unsupported metric.",
                "invalid_metrics": [metric],
                "valid_metrics": self.catalog.list_metrics(),
            }
        max_rows = _coerce_limit(limit, default=10, minimum=1, maximum=25)
        rows = self.repo.get_metric_leaders(metric_def.key, limit=max_rows)
        return {
            "status": "ok",
            "metric": metric_def.to_public_dict(),
            "rows": rows,
        }
