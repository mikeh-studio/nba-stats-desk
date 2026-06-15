from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.catalog import (
    DEFAULT_METRIC_KEYS,
    DEFAULT_TIER,
    load_semantic_catalog,
)
from app.agent.tools import StatsToolRunner


class ToolFakeRepository:
    def search_players(self, query: str, limit: int = 10) -> list[dict]:
        return [
            {
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "latest_team_abbr": "PHI",
                "games_sampled": 12,
                "sample_status": "ready",
                "overall_rank": 12,
            }
        ][:limit]

    def get_player_detail(self, player_id: int) -> dict | None:
        if player_id != 7:
            return None
        return {
            "player": {
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "team_abbr": "PHI",
                "games_sampled": 12,
                "sample_status": "ready",
                "overall_rank": 12,
            },
            "sample": {"games_sampled": 12, "is_qualified": True},
            "availability_state": "fresh",
            "availability_reason": None,
            "reason_summary": "recent box score production: +5.2",
            "trend": {"status": "rising", "delta": 6.4},
            "recent_form": [],
            "stat_percentiles": [
                {"key": "pts", "label": "PTS", "average": 25.8, "percentile": 91.0}
            ],
            "chart_baselines": {},
            "trends": [{"stat": "PTS", "label": "PTS", "delta": 6.4}],
            "game_log": self.get_player_game_log(7, limit=2),
            "archetype": {"archetype_label": "Primary Creator"},
            "similar_players": [{"player_id": 11, "player_name": "Jalen Brunson"}],
        }

    def get_player_game_log(
        self,
        player_id: int,
        limit: int = 30,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict | None:
        if player_id != 7:
            return None
        games = [
            {
                "game_date": "2026-02-01",
                "matchup": "PHI vs. NYK",
                "wl": "W",
                "team_abbr": "PHI",
                "opponent_abbr": "NYK",
                "pts": "28",
                "reb": "5",
                "ast": "7",
                "stl": "1",
                "blk": "0",
                "tov": "2",
            },
            {
                "game_date": "2026-02-03",
                "matchup": "PHI @ BOS",
                "wl": "L",
                "team_abbr": "PHI",
                "opponent_abbr": "BOS",
                "pts": "31",
                "reb": "4",
                "ast": "8",
                "stl": "2",
                "blk": "1",
                "tov": "3",
            },
            {
                "game_date": "2026-02-10",
                "matchup": "PHI vs. LAL",
                "wl": "W",
                "team_abbr": "PHI",
                "opponent_abbr": "LAL",
                "pts": "24",
                "reb": "3",
                "ast": "6",
                "stl": "1",
                "blk": "0",
                "tov": "1",
            },
        ]
        if start_date:
            games = [game for game in games if game["game_date"] >= start_date]
        if end_date:
            games = [game for game in games if game["game_date"] <= end_date]
        games = games[:limit]
        return {
            "player_id": 7,
            "player_name": "Tyrese Maxey",
            "season": "2025-26",
            "games": games,
            "date_range": {"start_date": start_date, "end_date": end_date},
        }

    def get_metric_leaders(self, metric: str, limit: int = 10) -> list[dict]:
        return [
            {
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "metric_key": metric,
                "metric_label": metric.upper(),
                "metric_value": 28.4,
                "percentile": 91.0,
            }
        ][:limit]

    def get_player_metric_percentile(
        self, player_id: int, metric: str, min_games: int = 5
    ) -> dict | None:
        if player_id != 7:
            return None
        return {
            "season": "2025-26",
            "player_id": 7,
            "player_name": "Tyrese Maxey",
            "team_abbr": "PHI",
            "games_sampled": 12,
            "metric_key": metric,
            "metric_label": "Attributed Points",
            "metric_value": 42.2,
            "min_games": min_games,
            "cohort_rank": 8,
            "percentile": 94.0,
            "cohort_size": 100,
            "cohort_avg": 24.0,
            "player_count": 500,
            "max_games_sampled": 82,
            "in_requested_cohort": True,
        }


def test_semantic_catalog_resolves_typo_aliases() -> None:
    catalog = load_semantic_catalog()

    assert catalog.resolve_metric("rbs").key == "reb"
    assert catalog.resolve_metric("blcks").key == "blk"
    assert catalog.resolve_metric("turnovers").key == "tov"
    points_created = catalog.resolve_metric("points + assists * 2")
    assert points_created.key == "points_created"
    assert points_created.formula == "pts + ast * 2"
    assert points_created.formula_variables == ("ast", "pts")


def test_agent_game_log_tool_returns_chart_payload() -> None:
    runner = StatsToolRunner(ToolFakeRepository())

    payload = runner.get_player_game_log(
        7,
        ["points", "assists", "attributed_points"],
        limit=2,
    )

    assert payload["status"] == "ok"
    assert payload["rows"][0]["metrics"] == {
        "pts": 28.0,
        "ast": 7.0,
        "points_created": 42.0,
    }
    assert payload["charts"][0]["type"] == "line"
    assert payload["charts"][0]["series"][0]["label"] == "PTS"
    assert payload["charts"][0]["series"][2]["label"] == "Attributed Points"
    assert payload["charts"][0]["series"][2]["points"][1]["y"] == 47.0


def test_agent_game_log_tool_applies_date_range_filter() -> None:
    runner = StatsToolRunner(ToolFakeRepository())

    payload = runner.get_player_game_log(
        7,
        ["points", "assists"],
        limit=10,
        start_date="2026-02-03",
        end_date="2026-02-03",
    )

    assert payload["status"] == "ok"
    assert payload["date_range"] == {
        "start_date": "2026-02-03",
        "end_date": "2026-02-03",
    }
    assert payload["games_returned"] == 1
    assert payload["rows"][0]["game_date"] == "2026-02-03"
    assert payload["rows"][0]["metrics"] == {"pts": 31.0, "ast": 8.0}


def test_agent_game_log_tool_rejects_invalid_date_range() -> None:
    runner = StatsToolRunner(ToolFakeRepository())

    payload = runner.get_player_game_log(
        7,
        ["points"],
        limit=10,
        start_date="2026-02-10",
        end_date="2026-02-03",
    )

    assert payload["status"] == "error"
    assert payload["message"] == "start_date must be on or before end_date."


def test_agent_trends_tool_computes_points_created_from_game_log() -> None:
    runner = StatsToolRunner(ToolFakeRepository())

    payload = runner.get_player_trends(7, ["attributed_points"])

    assert payload["status"] == "ok"
    assert payload["trends"][0]["stat"] == "POINTS_CREATED"
    assert payload["trends"][0]["formula"] == "PTS + AST * 2"
    assert payload["trends"][0]["recent_avg"] == 44.5
    assert payload["charts"][0]["series"][0]["points"] == [
        {
            "x": "2026-02-01",
            "y": 42.0,
            "meta": "PHI vs. NYK W AST 7 · PTS 28 formula PTS + AST * 2",
        },
        {
            "x": "2026-02-03",
            "y": 47.0,
            "meta": "PHI @ BOS L AST 8 · PTS 31 formula PTS + AST * 2",
        },
    ]


def test_agent_trends_tool_computes_from_date_filtered_game_log() -> None:
    runner = StatsToolRunner(ToolFakeRepository())

    payload = runner.get_player_trends(
        7,
        ["points"],
        start_date="2026-02-03",
        end_date="2026-02-10",
    )

    assert payload["status"] == "ok"
    assert payload["date_range"] == {
        "start_date": "2026-02-03",
        "end_date": "2026-02-10",
    }
    assert payload["trends"][0]["recent_games"] == 2
    assert payload["trends"][0]["recent_avg"] == 27.5
    assert payload["charts"][0]["series"][0]["points"] == [
        {"x": "2026-02-03", "y": 31.0, "meta": "PHI @ BOS L"},
        {"x": "2026-02-10", "y": 24.0, "meta": "PHI vs. LAL W"},
    ]


def test_metric_tiers_assign_every_metric_a_tier() -> None:
    catalog = load_semantic_catalog()

    assert catalog.tier_keys(1) == ("pts", "reb", "ast")
    assert catalog.tier_keys(2) == ("stl", "blk", "tov")
    assert catalog.tier_keys(3) == ("fg3m", "min")
    assert catalog.tier_keys(4) == ("fantasy_points_simple", "points_created")
    # No metric should silently fall through to the untiered bucket.
    assert all(metric.tier <= 4 for metric in catalog.metrics.values())


def test_default_metric_keys_track_tiers_one_and_two() -> None:
    catalog = load_semantic_catalog()

    assert catalog.default_metric_keys() == DEFAULT_METRIC_KEYS
    assert catalog.default_metric_keys(max_tier=DEFAULT_TIER) == DEFAULT_METRIC_KEYS
    assert catalog.default_metric_keys(max_tier=1) == ("pts", "reb", "ast")


def test_resolve_metrics_expands_generic_all_stats_to_defaults() -> None:
    catalog = load_semantic_catalog()

    selected, invalid = catalog.resolve_metrics(["all individual stats"])

    assert invalid == []
    assert [metric.key for metric in selected] == list(DEFAULT_METRIC_KEYS)


def test_resolve_metrics_drops_unknown_keeps_valid() -> None:
    catalog = load_semantic_catalog()

    selected, invalid = catalog.resolve_metrics(["points", "made up stat"])

    assert [metric.key for metric in selected] == ["pts"]
    assert invalid == ["made up stat"]


def test_resolve_metrics_falls_back_to_defaults_when_all_unknown() -> None:
    catalog = load_semantic_catalog()

    selected, invalid = catalog.resolve_metrics(["made up stat"])

    assert [metric.key for metric in selected] == list(DEFAULT_METRIC_KEYS)
    assert invalid == ["made up stat"]


def test_agent_game_log_tool_answers_generic_stats_request() -> None:
    runner = StatsToolRunner(ToolFakeRepository())

    payload = runner.get_player_game_log(7, ["individual stats"], limit=2)

    assert payload["status"] == "ok"
    assert payload["ignored_metrics"] == []
    assert [metric["key"] for metric in payload["metrics"]] == list(DEFAULT_METRIC_KEYS)


def test_agent_trends_tool_ignores_unknown_metric_without_failing() -> None:
    runner = StatsToolRunner(ToolFakeRepository())

    payload = runner.get_player_trends(7, ["points", "nonsense"])

    assert payload["status"] == "ok"
    assert payload["ignored_metrics"] == ["nonsense"]


class TrendFakeRepository(ToolFakeRepository):
    """20-game window where scoring jumps from ~20 to ~32 at the midpoint."""

    def get_player_game_log(
        self,
        player_id: int,
        limit: int = 30,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict | None:
        if player_id != 7:
            return None
        games = []
        for index in range(20):
            pts = 20 if index < 10 else 32
            games.append(
                {
                    "game_date": f"2026-01-{index + 1:02d}",
                    "matchup": "PHI vs. NYK",
                    "wl": "W",
                    "team_abbr": "PHI",
                    "opponent_abbr": "NYK",
                    "pts": str(pts),
                    "reb": "5",
                    "ast": "5",
                    "stl": "1",
                    "blk": "0",
                    "tov": "2",
                }
            )
        if start_date:
            games = [game for game in games if game["game_date"] >= start_date]
        if end_date:
            games = [game for game in games if game["game_date"] <= end_date]
        if limit:
            games = games[-limit:]
        return {
            "player_id": 7,
            "player_name": "Tyrese Maxey",
            "season": "2025-26",
            "games": games,
            "date_range": {"start_date": start_date, "end_date": end_date},
        }


def test_trends_split_window_in_half_and_flag_change_point() -> None:
    runner = StatsToolRunner(TrendFakeRepository())

    payload = runner.get_player_trends(7, ["points"], limit=20)

    assert payload["status"] == "ok"
    row = payload["trends"][0]
    # First-half vs second-half, spanning the whole 20-game window.
    assert row["window_games"] == 20
    assert row["prior_avg"] == 20.0
    assert row["recent_avg"] == 32.0
    assert row["delta"] == 12.0
    assert row["trend_shape"] == "rising"
    assert row["best"] == 32.0
    assert row["worst"] == 20.0
    change_point = row["change_point"]
    assert change_point is not None
    assert change_point["split_game_number"] == 11
    assert change_point["before_avg"] == 20.0
    assert change_point["after_avg"] == 32.0
    assert change_point["delta"] == 12.0
    # The trend chart carries a rolling-average overlay alongside the raw line.
    series_keys = {series["key"] for series in payload["charts"][0]["series"]}
    assert {"pts", "pts_avg3"} <= series_keys


def test_change_point_ignores_day_to_day_noise() -> None:
    from app.agent.tools import _detect_change_point, _stdev

    values = [20, 22, 19, 21, 20, 22, 21, 19, 20, 21]
    pairs = [(f"2026-01-{i + 1:02d}", float(v)) for i, v in enumerate(values)]

    assert _detect_change_point(pairs, _stdev([float(v) for v in values])) is None


def test_trend_shape_reads_flat_when_swing_is_within_noise() -> None:
    from app.agent.tools import _classify_shape, _linear_slope, _stdev

    values = [20.0, 22.0, 19.0, 21.0, 20.0, 22.0, 21.0, 19.0, 20.0, 21.0]
    slope = _linear_slope(values)

    assert _classify_shape(slope, _stdev(values), len(values)) == "flat"


def test_rolling_average_uses_trailing_window() -> None:
    from app.agent.tools import _rolling_average

    assert _rolling_average([10.0, 20.0, 30.0, 40.0], window=3) == [20.0, 30.0]
    assert _rolling_average([10.0, 20.0], window=3) == []


def test_agent_opponent_splits_identifies_toughest_matchup() -> None:
    runner = StatsToolRunner(ToolFakeRepository())

    payload = runner.get_player_opponent_splits(7, ["points"], limit=20)

    assert payload["status"] == "ok"
    assert payload["primary_metric"]["key"] == "pts"
    assert payload["games_returned"] == 3
    assert payload["overall_averages"]["pts"] == 27.67
    # Worst scoring game is the 24-point night vs LAL.
    toughest = payload["toughest_opponent"]
    assert toughest["opponent_abbr"] == "LAL"
    assert toughest["metrics"]["pts"] == 24.0
    assert toughest["primary_delta_vs_overall"] == -3.67
    assert toughest["record"] == "1-0"
    assert [row["opponent_abbr"] for row in payload["opponents"]] == [
        "LAL",
        "NYK",
        "BOS",
    ]
    assert payload["charts"][0]["type"] == "bar"


def test_agent_opponent_splits_defaults_metrics_for_generic_request() -> None:
    runner = StatsToolRunner(ToolFakeRepository())

    payload = runner.get_player_opponent_splits(7, ["all stats"], limit=20)

    assert payload["status"] == "ok"
    assert payload["ignored_metrics"] == []
    assert payload["primary_metric"]["key"] == "pts"


def test_agent_ranking_tool_rejects_unknown_metric() -> None:
    runner = StatsToolRunner(ToolFakeRepository())

    payload = runner.search_rankings("pts; drop table", limit=5)

    assert payload["status"] == "error"
    assert payload["invalid_metrics"] == ["pts; drop table"]


def test_agent_calculates_points_created_percentile_alias() -> None:
    runner = StatsToolRunner(ToolFakeRepository())

    payload = runner.calculate_player_percentile(
        7,
        "points + assists * 2",
        min_games=10,
    )

    assert payload["status"] == "ok"
    assert payload["metric"]["key"] == "points_created"
    assert payload["metric_value"] == 42.2
    assert payload["percentile"] == 94.0
