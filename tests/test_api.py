from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, get_settings
from app.main import app, get_agent_client, get_repository
from app.repository import WarehouseRepository
from app.telemetry import LOGGER_NAME


class FakeRepository(WarehouseRepository):
    def get_dashboard(self, as_of_date: str | None = None) -> dict:
        selected_date = as_of_date or "2026-02-11"
        return {
            "selected_as_of_date": selected_date,
            "date_options": [
                {
                    "value": "2026-02-11",
                    "label": "Wed Feb 11",
                    "is_selected": selected_date == "2026-02-11",
                },
                {
                    "value": "2026-02-10",
                    "label": "Tue Feb 10",
                    "is_selected": selected_date == "2026-02-10",
                },
                {
                    "value": "2026-02-09",
                    "label": "Mon Feb 09",
                    "is_selected": selected_date == "2026-02-09",
                },
                {
                    "value": "2026-02-08",
                    "label": "Sun Feb 08",
                    "is_selected": selected_date == "2026-02-08",
                },
                {
                    "value": "2026-02-07",
                    "label": "Sat Feb 07",
                    "is_selected": selected_date == "2026-02-07",
                },
                {
                    "value": "2026-02-06",
                    "label": "Fri Feb 06",
                    "is_selected": selected_date == "2026-02-06",
                },
                {
                    "value": "2026-02-05",
                    "label": "Thu Feb 05",
                    "is_selected": selected_date == "2026-02-05",
                },
            ],
            "signals": [
                {
                    "player_id": 7,
                    "player_name": "Tyrese Maxey",
                    "latest_team_abbr": "PHI",
                    "overall_rank": 12,
                    "recommendation_score": 91.2,
                    "category_strengths": "PTS, AST, 3PM",
                    "category_risks": "FG%",
                    "reason_summary": "recent box score production: +5.2 | next 7 days: 4",
                    "trend_status": "rising",
                    "trend_direction": "up",
                    "headshot_url": "https://cdn.nba.com/headshots/nba/latest/1040x760/7.png",
                    "player_initials": "TM",
                    "top_improvements": [
                        {"label": "PTS", "delta": 6.4},
                        {"label": "AST", "delta": 1.4},
                        {"label": "3PM", "delta": 0.6},
                    ],
                }
            ],
            "rankings": [
                {
                    "season": "2025-26",
                    "player_id": 7,
                    "player_name": "Tyrese Maxey",
                    "overall_rank": 12,
                    "recommendation_score": 91.2,
                }
            ],
            "trends": [
                {
                    "season": "2025-26",
                    "player_id": 7,
                    "player_name": "Tyrese Maxey",
                    "trend_status": "rising",
                    "trend_direction": "up",
                    "trend_delta": 6.4,
                }
            ],
            "opportunity": [
                {
                    "season": "2025-26",
                    "player_id": 7,
                    "player_name": "Tyrese Maxey",
                    "games_next_7d": 4,
                    "back_to_backs_next_7d": 1,
                    "next_opponent_abbr": "NYK",
                    "opportunity_score": 84.0,
                }
            ],
        }

    def get_leaderboard(self, limit: int = 10) -> list[dict]:
        return [
            {
                "season": "2025-26",
                "game_date": "2026-02-10",
                "pts_leader": "Jayson Tatum",
                "pts_matchup": "BOS vs. NYK",
                "pts": 34,
                "reb_leader": "Karl-Anthony Towns",
                "reb": 14,
                "ast_leader": "Trae Young",
                "ast": 11,
            }
        ][:limit]

    def get_trends(self, limit: int = 10) -> list[dict]:
        return [
            {
                "season": "2025-26",
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "trend_status": "rising",
                "trend_delta": 6.4,
                "reason_summary": "recent box score production: +5.2",
            }
        ][:limit]

    def get_recommendations(
        self, limit: int = 10, insight_type: str | None = None
    ) -> list[dict]:
        items = [
            {
                "insight_id": "insight_1",
                "as_of_date": "2026-02-11",
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "insight_type": "waiver_add",
                "priority_score": 94.0,
                "confidence_score": 88.0,
                "category_focus": "PTS, AST, 3PM",
                "recommendation": "add",
                "title": "Tyrese Maxey is a high-priority add",
                "summary": "Minutes and assist creation are both trending up.",
            }
        ]
        if insight_type:
            items = [item for item in items if item["insight_type"] == insight_type]
        return items[:limit]

    def get_rankings(self, limit: int = 25) -> list[dict]:
        return [
            {
                "season": "2025-26",
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "overall_rank": 12,
                "recommendation_score": 91.2,
                "category_strengths": "PTS, AST, 3PM",
                "category_risks": "FG%",
                "games_next_7d": 4,
                "back_to_backs_next_7d": 1,
                "reason_summary": "recent box score production: +5.2",
            }
        ][:limit]

    def search_players(self, query: str, limit: int = 10) -> list[dict]:
        if "bridges" in query.lower():
            return [
                {
                    "player_id": 9,
                    "player_name": "Mikal Bridges",
                    "latest_season": "2025-26",
                    "latest_team_abbr": "NYK",
                    "latest_game_date": "2026-02-10",
                    "games_sampled": 6,
                    "qualification_games": 5,
                    "is_qualified": True,
                    "sample_status": "limited_sample",
                    "overall_rank": None,
                    "recommendation_score": None,
                    "headshot_url": "https://cdn.nba.com/headshots/nba/latest/1040x760/9.png",
                    "player_initials": "MB",
                    "last_seen_at_utc": "2026-02-10T13:00:00+00:00",
                }
            ][:limit]
        return [
            {
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "latest_season": "2025-26",
                "latest_team_abbr": "PHI",
                "latest_game_date": "2026-02-10",
                "games_sampled": 12,
                "qualification_games": 5,
                "is_qualified": True,
                "sample_status": "ready",
                "overall_rank": 12,
                "recommendation_score": 91.2,
                "headshot_url": "https://cdn.nba.com/headshots/nba/latest/1040x760/7.png",
                "player_initials": "TM",
                "last_seen_at_utc": "2026-02-10T13:00:00+00:00",
            }
        ][:limit]

    def get_player_detail(self, player_id: int) -> dict | None:
        if player_id == 7:
            return {
                "player": {
                    "season": "2025-26",
                    "player_id": 7,
                    "player_name": "Tyrese Maxey",
                    "headshot_url": "https://cdn.nba.com/headshots/nba/latest/1040x760/7.png",
                    "player_initials": "TM",
                    "team_abbr": "PHI",
                    "latest_game_date": "2026-02-10",
                    "overall_rank": 12,
                    "recommendation_score": 91.2,
                    "recommendation_tier": "hold",
                    "category_strengths": "PTS, AST, 3PM",
                    "category_risks": "FG%",
                    "is_ranked": True,
                    "games_sampled": 12,
                    "sample_status": "ready",
                    "is_qualified": True,
                },
                "sample": {
                    "games_sampled": 12,
                    "qualification_games": 5,
                    "is_qualified": True,
                    "sample_status": "ready",
                    "sample_warning": None,
                },
                "availability_state": "fresh",
                "availability_reason": None,
                "reason_summary": "recent box score production: +5.2 | next 7 days: 4",
                "trend": {
                    "status": "rising",
                    "delta": 6.4,
                    "pct_change": 29.1,
                },
                "panel_states": {
                    "recent_form": "fresh",
                    "category_profile": "fresh",
                    "opportunity": "fresh",
                    "archetype": "fresh",
                    "similarity": "fresh",
                    "stat_percentiles": "fresh",
                    "game_log": "fresh",
                    "trends": "fresh",
                },
                "recent_form": [
                    {
                        "window_key": "last_5",
                        "window_label": "Last 5",
                        "games_in_window": 5,
                        "window_games_expected": 5,
                        "state": "fresh",
                        "state_reason": None,
                        "avg_pts": 28.4,
                        "avg_reb": 4.8,
                        "avg_ast": 7.4,
                        "avg_stl": 1.3,
                        "avg_blk": 0.3,
                        "avg_fg3m": 3.1,
                        "avg_tov": 2.1,
                        "avg_minutes": 36.1,
                        "fantasy_proxy": 45.2,
                    },
                    {
                        "window_key": "prior_5",
                        "window_label": "Prior 5",
                        "games_in_window": 5,
                        "window_games_expected": 5,
                        "state": "fresh",
                        "state_reason": None,
                        "avg_pts": 22.0,
                        "avg_reb": 4.1,
                        "avg_ast": 6.0,
                        "avg_stl": 1.0,
                        "avg_blk": 0.2,
                        "avg_fg3m": 2.5,
                        "avg_tov": 2.4,
                        "avg_minutes": 34.0,
                        "fantasy_proxy": 40.0,
                    },
                    {
                        "window_key": "last_10",
                        "window_label": "Last 10",
                        "games_in_window": 10,
                        "window_games_expected": 10,
                        "state": "fresh",
                        "state_reason": None,
                        "avg_pts": 25.2,
                        "avg_reb": 4.4,
                        "avg_ast": 6.7,
                        "avg_stl": 1.1,
                        "avg_blk": 0.2,
                        "avg_fg3m": 2.8,
                        "avg_tov": 2.2,
                        "avg_minutes": 35.0,
                        "fantasy_proxy": 42.6,
                    },
                ],
                "category_profile": [
                    {
                        "category": "AST",
                        "impact_score": 1.8,
                        "category_tier": "plus",
                        "category_direction": "up",
                    }
                ],
                "stat_percentiles": [
                    {
                        "key": "pts",
                        "label": "PTS",
                        "average": 25.8,
                        "percentile": 91.0,
                        "bar_width": 91.0,
                        "direction": "higher",
                    },
                    {
                        "key": "tov",
                        "label": "Ball Security",
                        "average": 2.2,
                        "percentile": 54.0,
                        "bar_width": 54.0,
                        "direction": "lower",
                    },
                ],
                "chart_baselines": {
                    "pts": {
                        "key": "pts",
                        "label": "PTS",
                        "value": 12.4,
                        "direction": "higher",
                    },
                    "reb": {
                        "key": "reb",
                        "label": "REB",
                        "value": 4.5,
                        "direction": "higher",
                    },
                    "ast": {
                        "key": "ast",
                        "label": "AST",
                        "value": 2.8,
                        "direction": "higher",
                    },
                    "stl": {
                        "key": "stl",
                        "label": "STL",
                        "value": 0.8,
                        "direction": "higher",
                    },
                    "blk": {
                        "key": "blk",
                        "label": "BLK",
                        "value": 0.5,
                        "direction": "higher",
                    },
                    "tov": {
                        "key": "tov",
                        "label": "Ball Security",
                        "value": 1.3,
                        "direction": "lower",
                    },
                },
                "game_log": self.get_player_game_log(7),
                "trends": [
                    {
                        "stat": "PTS",
                        "label": "PTS",
                        "recent_games": 5,
                        "prior_games": 5,
                        "recent_avg": 28.4,
                        "prior_avg": 22.0,
                        "delta": 6.4,
                        "pct_change": 29.1,
                        "direction_is_good": True,
                    }
                ],
                "opportunity": {
                    "games_next_7d": 4,
                    "back_to_backs_next_7d": 1,
                    "next_opponent": "NYK",
                    "next_game_date": "2026-02-13",
                    "opportunity_score": 84.0,
                },
                "archetype": {
                    "state": "fresh",
                    "archetype_id": "cluster_2",
                    "archetype_label": "Primary Creator",
                    "cluster_confidence": 0.92,
                    "top_traits": ["playmaking", "usage share", "scoring volume"],
                    "summary": "Primary Creator driven by playmaking, usage share, scoring volume.",
                },
                "similarity_reason": None,
                "similar_players": [
                    {
                        "player_id": 11,
                        "player_name": "Jalen Brunson",
                        "team_abbr": "NYK",
                        "headshot_url": "https://cdn.nba.com/headshots/nba/latest/1040x760/11.png",
                        "player_initials": "JB",
                        "similarity_score": 0.91,
                        "archetype_label": "Primary Creator",
                        "shared_traits": ["playmaking", "usage share"],
                        "contrasting_traits": ["three-point volume"],
                    }
                ],
            }
        if player_id == 9:
            return {
                "player": {
                    "season": "2025-26",
                    "player_id": 9,
                    "player_name": "Mikal Bridges",
                    "headshot_url": "https://cdn.nba.com/headshots/nba/latest/1040x760/9.png",
                    "player_initials": "MB",
                    "team_abbr": "NYK",
                    "latest_game_date": None,
                    "overall_rank": None,
                    "recommendation_score": None,
                    "recommendation_tier": None,
                    "category_strengths": None,
                    "category_risks": None,
                    "is_ranked": False,
                    "games_sampled": 6,
                    "sample_status": "limited_sample",
                    "is_qualified": True,
                },
                "sample": {
                    "games_sampled": 6,
                    "qualification_games": 5,
                    "is_qualified": True,
                    "sample_status": "limited_sample",
                    "sample_warning": "Limited sample: percentiles are available, but still volatile.",
                },
                "availability_state": "unavailable",
                "availability_reason": "Not currently ranked",
                "reason_summary": None,
                "trend": {
                    "status": "unavailable",
                    "delta": None,
                    "pct_change": None,
                },
                "panel_states": {
                    "recent_form": "unavailable",
                    "category_profile": "unavailable",
                    "opportunity": "unavailable",
                    "archetype": "unavailable",
                    "similarity": "unavailable",
                    "stat_percentiles": "unavailable",
                    "game_log": "unavailable",
                    "trends": "unavailable",
                },
                "recent_form": [],
                "category_profile": [],
                "stat_percentiles": [],
                "chart_baselines": {},
                "game_log": self.get_player_game_log(9),
                "trends": [],
                "opportunity": None,
                "archetype": {
                    "state": "unavailable",
                    "archetype_id": None,
                    "archetype_label": None,
                    "cluster_confidence": None,
                    "top_traits": [],
                    "summary": None,
                },
                "similarity_reason": "Similarity profile is unavailable.",
                "similar_players": [],
            }
        return None

    def get_player_game_log(
        self,
        player_id: int,
        limit: int = 30,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict | None:
        if player_id == 7:
            games = [
                {
                    "game_id": f"0022500{d}",
                    "season": "2025-26",
                    "game_date": f"2026-02-{str(d).zfill(2)}",
                    "player_id": 7,
                    "player_name": "Tyrese Maxey",
                    "team_abbr": "PHI",
                    "opponent_abbr": "NYK",
                    "home_away": "home",
                    "matchup": "PHI vs. NYK",
                    "wl": "W",
                    "min": "36.0",
                    "pts": str(20 + d),
                    "reb": "5",
                    "ast": "7",
                    "stl": "1",
                    "blk": "0",
                    "tov": "2",
                    "fg3m": "3",
                    "fgm": "9",
                    "fga": "18",
                    "fg_pct": "0.500",
                    "ftm": "4",
                    "fta": "5",
                    "ft_pct": "0.800",
                    "fantasy_points_simple": "42.5",
                }
                for d in range(1, min(limit, 5) + 1)
            ]
            if start_date:
                games = [game for game in games if game["game_date"] >= start_date]
            if end_date:
                games = [game for game in games if game["game_date"] <= end_date]
            return {
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "season": "2025-26",
                "games": games,
                "games_returned": len(games),
                "limit": limit,
                "order": "chronological",
                "date_range": {"start_date": start_date, "end_date": end_date},
            }
        if player_id == 9:
            return {
                "player_id": 9,
                "player_name": "Mikal Bridges",
                "season": "2025-26",
                "games": [],
                "games_returned": 0,
                "limit": limit,
                "order": "chronological",
                "date_range": {"start_date": start_date, "end_date": end_date},
            }
        return None

    def get_recent_performance_dates(self) -> list[dict]:
        return [
            {"value": "2026-02-10", "label": "Tue Feb 10"},
            {"value": "2026-02-09", "label": "Mon Feb 09"},
        ]

    def get_recent_performance_games(
        self, *, game_date: str | None = None
    ) -> list[dict]:
        rows = [
            {
                "game_id": "002250010",
                "game_date": "2026-02-10",
                "matchup": "NYK @ PHI",
                "teams": "NYK / PHI",
                "home_team_abbr": "PHI",
                "away_team_abbr": "NYK",
                "home_team_pts": 112,
                "away_team_pts": 108,
                "players_played": 20,
            }
        ]
        if game_date:
            rows = [row for row in rows if row["game_date"] == game_date]
        return rows

    def _performance_row(self) -> dict:
        return {
            "game_id": "002250010",
            "game_date": "2026-02-10",
            "player_id": 7,
            "player_name": "Tyrese Maxey",
            "team_abbr": "PHI",
            "opponent_abbr": "NYK",
            "home_away": "home",
            "matchup": "PHI vs. NYK",
            "wl": "W",
            "minutes": 36.0,
            "games_sampled": 12,
            "performance_score": 2.4,
            "performance_status": "above",
            "above_count": 3,
            "below_count": 1,
            "headshot_url": "https://cdn.nba.com/headshots/nba/latest/1040x760/7.png",
            "player_initials": "TM",
            "metrics": [
                {
                    "key": "pts",
                    "label": "PTS",
                    "value": 31,
                    "season_average": 25.8,
                    "delta": 5.2,
                    "delta_pct": 20.2,
                    "status": "above",
                    "percentile": 82.0,
                    "range": {
                        "p10": 18,
                        "p25": 22,
                        "median": 26,
                        "p75": 30,
                        "p90": 34,
                    },
                },
                {
                    "key": "reb",
                    "label": "REB",
                    "value": 5,
                    "season_average": 4.4,
                    "delta": 0.6,
                    "delta_pct": 13.6,
                    "status": "above",
                    "percentile": 66.7,
                    "range": {
                        "p10": 2,
                        "p25": 3,
                        "median": 4,
                        "p75": 6,
                        "p90": 8,
                    },
                },
                {
                    "key": "ast",
                    "label": "AST",
                    "value": 7,
                    "season_average": 6.7,
                    "delta": 0.3,
                    "delta_pct": 4.5,
                    "status": "above",
                    "percentile": 58.3,
                    "range": {
                        "p10": 4,
                        "p25": 5,
                        "median": 7,
                        "p75": 9,
                        "p90": 11,
                    },
                },
                {
                    "key": "stl",
                    "label": "STL",
                    "value": 1,
                    "season_average": 1.1,
                    "delta": -0.1,
                    "delta_pct": -9.1,
                    "status": "below",
                    "percentile": 54.0,
                    "range": {
                        "p10": 0,
                        "p25": 0,
                        "median": 1,
                        "p75": 2,
                        "p90": 3,
                    },
                },
                {
                    "key": "blk",
                    "label": "BLK",
                    "value": 0,
                    "season_average": 0.2,
                    "delta": -0.2,
                    "delta_pct": -100.0,
                    "status": "below",
                    "percentile": 50.0,
                    "range": {
                        "p10": 0,
                        "p25": 0,
                        "median": 0,
                        "p75": 0,
                        "p90": 1,
                    },
                },
            ],
        }

    def _performance_trend(self) -> dict:
        return {
            "window_days": 30,
            "stats": [
                {"key": "pts", "label": "PTS", "season_average": 25.8},
                {"key": "reb", "label": "REB", "season_average": 4.4},
                {"key": "ast", "label": "AST", "season_average": 6.7},
                {"key": "stl", "label": "STL", "season_average": 1.1},
                {"key": "blk", "label": "BLK", "season_average": 0.2},
            ],
            "points": [
                {
                    "game_id": "002250008",
                    "game_date": "2026-02-08",
                    "matchup": "PHI @ BOS",
                    "minutes": 35.0,
                    "pts": 26,
                    "reb": 4,
                    "ast": 6,
                    "stl": 1,
                    "blk": 0,
                },
                {
                    "game_id": "002250010",
                    "game_date": "2026-02-10",
                    "matchup": "PHI vs. NYK",
                    "minutes": 36.0,
                    "pts": 31,
                    "reb": 5,
                    "ast": 7,
                    "stl": 1,
                    "blk": 0,
                },
            ],
        }

    def get_recent_performance_players(
        self,
        *,
        game_date: str,
        game_id: str | None = None,
        limit: int = 240,
    ) -> list[dict]:
        if game_date != "2026-02-10":
            return []
        row = self._performance_row()
        if game_id and game_id != row["game_id"]:
            return []
        return [row][:limit]

    def get_recent_performance_player(
        self, player_id: int, *, game_id: str
    ) -> dict | None:
        row = self._performance_row()
        if player_id == row["player_id"] and game_id == row["game_id"]:
            row = dict(row)
            row["trend_30d"] = self._performance_trend()
            return row
        return None

    def get_metric_leaders(self, metric: str, limit: int = 10) -> list[dict]:
        rows = [
            {
                "season": "2025-26",
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "team_abbr": "PHI",
                "games_sampled": 12,
                "sample_status": "ready",
                "metric_key": metric,
                "metric_label": metric.upper(),
                "metric_value": 28.4,
                "percentile": 91.0,
            }
        ]
        return rows[:limit]

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

    def get_compare(
        self,
        player_a_id: int,
        player_b_id: int,
        *,
        window: str = "last_5",
        focus: str = "balanced",
    ) -> dict:
        window_labels = {
            "last_3": "Last 3",
            "last_5": "Last 5",
            "last_7": "Last 7",
            "prior_5": "Prior 5",
            "last_10": "Last 10",
        }
        expected_games = {
            "last_3": 3,
            "last_5": 5,
            "last_7": 7,
            "prior_5": 5,
            "last_10": 10,
        }
        focus_rows = {
            "balanced": [
                "Box Score Index",
                "Minutes",
                "PTS",
                "REB",
                "AST",
                "STL",
                "BLK",
                "3PM",
                "TOV",
            ],
            "scoring": [
                "Box Score Index",
                "PTS",
                "3PM",
                "Minutes",
                "AST",
                "REB",
                "STL",
                "BLK",
                "TOV",
            ],
        }
        row_labels = focus_rows.get(focus, focus_rows["balanced"])

        def metric_rows(
            metric_values: dict[str, float | None],
        ) -> list[dict[str, object]]:
            label_to_key = {
                "Box Score Index": "fantasy_proxy_score",
                "Minutes": "avg_min",
                "PTS": "avg_pts",
                "REB": "avg_reb",
                "AST": "avg_ast",
                "STL": "avg_stl",
                "BLK": "avg_blk",
                "3PM": "avg_fg3m",
                "TOV": "avg_tov",
            }
            return [
                {
                    "label": label,
                    "value": metric_values.get(label_to_key[label]),
                    "is_focus": index < 3,
                }
                for index, label in enumerate(row_labels)
            ]

        player_a_metrics = {
            "fantasy_proxy_score": 45.2,
            "avg_min": 36.1,
            "avg_pts": 28.4,
            "avg_reb": 4.8,
            "avg_ast": 7.4,
            "avg_stl": 1.3,
            "avg_blk": 0.3,
            "avg_fg3m": 3.1,
            "avg_tov": 2.1,
        }
        player_b_metrics = {
            "fantasy_proxy_score": None,
            "avg_min": None,
            "avg_pts": None,
            "avg_reb": None,
            "avg_ast": None,
            "avg_stl": None,
            "avg_blk": None,
            "avg_fg3m": None,
            "avg_tov": None,
        }
        return {
            "season": "2025-26",
            "window": window,
            "window_label": window_labels[window],
            "focus": focus,
            "focus_label": focus.title(),
            "focus_description": "Focus description for tests.",
            "similarity": {
                "state": "fresh",
                "score": 0.81,
                "summary": "Archetypes diverge: Primary Creator vs Two-Way Wing. Current stat-profile similarity is 0.81.",
                "same_archetype": False,
                "archetype_labels": ["Primary Creator", "Two-Way Wing"],
                "shared_traits": ["scoring volume"],
                "contrasting_traits": ["rim protection"],
            },
            "comparison": {
                "player_a": {
                    "player_id": player_a_id,
                    "player_name": "Tyrese Maxey",
                    "headshot_url": "https://cdn.nba.com/headshots/nba/latest/1040x760/7.png",
                    "player_initials": "TM",
                    "latest_team_abbr": "PHI",
                    "latest_game_date": "2026-02-10",
                    "window": window,
                    "window_label": window_labels[window],
                    "state": "fresh",
                    "state_reason": None,
                    "games_in_window": expected_games[window],
                    "window_games_expected": expected_games[window],
                    "has_full_window": True,
                    "availability_state": "fresh",
                    "player_detail": self.get_player_detail(player_a_id),
                    "metrics": player_a_metrics,
                    "metric_rows": metric_rows(player_a_metrics),
                },
                "player_b": {
                    "player_id": player_b_id,
                    "player_name": "Mikal Bridges",
                    "headshot_url": "https://cdn.nba.com/headshots/nba/latest/1040x760/9.png",
                    "player_initials": "MB",
                    "latest_team_abbr": "NYK",
                    "latest_game_date": None,
                    "window": window,
                    "window_label": window_labels[window],
                    "state": "unavailable",
                    "state_reason": "Not currently ranked",
                    "games_in_window": None,
                    "window_games_expected": expected_games[window],
                    "has_full_window": False,
                    "availability_state": "unavailable",
                    "player_detail": self.get_player_detail(player_b_id),
                    "metrics": player_b_metrics,
                    "metric_rows": metric_rows(player_b_metrics),
                },
            },
        }

    def get_latest_analysis(self) -> dict | None:
        return {
            "snapshot_id": "202526_20260211",
            "snapshot_date": "2026-02-11",
            "created_at_utc": "2026-02-11T01:02:03+00:00",
            "season": "2025-26",
            "headline": "Tyrese Maxey headlines the 2025-26 trend watch",
            "dek": "Latest leaders from 2026-02-10 are anchored by Jayson Tatum in scoring.",
            "body": "Deterministic analysis body.",
            "trend_player": "Tyrese Maxey",
            "trend_stat": "PTS",
            "trend_delta": 6.4,
            "score_contribution": {
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "team_abbr": "PHI",
                "opponent_abbr": "NYK",
                "matchup": "PHI vs. NYK",
                "player_pts": 31,
                "team_pts": 112,
                "opponent_team_pts": 108,
                "player_points_share_of_team": 0.2768,
                "player_points_share_of_game": 0.1416,
                "scoring_margin": 4,
                "team_pts_qtr1": 28,
                "team_pts_qtr2": 24,
                "team_pts_qtr3": 30,
                "team_pts_qtr4": 30,
                "team_pts_ot_total": 0,
                "game_date": "2026-02-10",
            },
            "player_context": {
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "team_abbr": "PHI",
                "team_name": "76ers",
                "position": "G",
                "height": "6-2",
                "weight": 200,
                "roster_status": True,
                "season_exp": 5,
                "draft_year": "2020",
                "draft_round": "1",
                "draft_number": "21",
            },
            "freshness_ts": "2026-02-10T13:00:00+00:00",
            "source_run_id": "manual__2026-02-11T01:02:03+00:00",
        }

    def get_latest_successful_run(self) -> dict | None:
        return {"season": "2025-26", "finished_at_utc": "2026-02-11T01:15:00+00:00"}

    def get_similarity_map(self) -> dict:
        return {
            "season": "2025-26",
            "players": [
                {
                    "player_id": 1,
                    "player_name": "Alpha Guard",
                    "team_abbr": "AAA",
                    "archetype_id": "cluster_0",
                    "archetype_label": "Scoring Guard",
                    "cluster_confidence": 0.82,
                    "top_traits": ["scoring volume", "true shooting"],
                    "games_sampled": 20,
                    "sample_status": "ready",
                    "x": 0.12,
                    "y": -0.34,
                    "z": 0.05,
                }
            ],
            "archetypes": [{"archetype_label": "Scoring Guard", "count": 1}],
            "axes": [
                {
                    "key": "proj_x",
                    "variance": 0.28,
                    "drivers": ["scoring volume", "usage"],
                },
                {"key": "proj_y", "variance": 0.19, "drivers": ["rim protection"]},
                {"key": "proj_z", "variance": 0.11, "drivers": ["playmaking"]},
            ],
        }

    def get_similarity_neighbors(self, player_id: int, *, limit: int = 6) -> dict:
        return {
            "state": "fresh",
            "reason": None,
            "player_id": player_id,
            "player_name": "Alpha Guard",
            "neighbors": [
                {
                    "player_id": 2,
                    "player_name": "Beta Guard",
                    "team_abbr": "BBB",
                    "archetype_label": "Scoring Guard",
                    "similarity_score": 0.91,
                    "shared_traits": ["scoring volume"],
                }
            ],
        }

    def get_health(self) -> dict:
        return {
            "season": "2025-26",
            "status": "fresh",
            "is_fresh": True,
            "checked_at_utc": "2026-02-11T02:00:00+00:00",
            "age_hours": 0.8,
            "threshold_hours": 36,
            "last_successful_finished_at_utc": "2026-02-11T01:15:00+00:00",
        }


class StaleRepository(FakeRepository):
    def get_health(self) -> dict:
        return {
            "season": "2025-26",
            "status": "stale",
            "is_fresh": False,
            "checked_at_utc": "2026-02-11T12:00:00+00:00",
            "age_hours": 48.0,
            "threshold_hours": 36,
            "last_successful_finished_at_utc": "2026-02-09T12:00:00+00:00",
        }


class MissingOpportunityRepository(FakeRepository):
    def get_dashboard(self, as_of_date: str | None = None) -> dict:
        payload = super().get_dashboard(as_of_date=as_of_date)
        payload["opportunity"] = []
        return payload

    def get_player_detail(self, player_id: int) -> dict | None:
        detail = super().get_player_detail(player_id)
        if detail is None:
            return None
        if player_id == 7:
            detail["panel_states"]["opportunity"] = "unavailable"
            detail["opportunity"] = None
        return detail


def _test_settings(**overrides: object) -> Settings:
    values = {
        "project_id": "local-project",
        "gold_dataset": "nba_gold",
        "metadata_dataset": "nba_metadata",
        "freshness_threshold_hours": 36,
        "max_search_results": 12,
        "openai_api_key": None,
        "openai_agent_model": "gpt-5.4-mini",
        "openai_agent_enabled": True,
        "agent_max_tool_calls": 6,
        "agent_rate_limit_per_minute": 12,
    }
    values.update(overrides)
    return Settings(**values)


class FakeOpenAIResponses:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(
                output=[
                    SimpleNamespace(
                        type="function_call",
                        name="resolve_player",
                        arguments=json.dumps({"name": "Tyrese Maxey", "limit": 5}),
                        call_id="call_1",
                    )
                ],
                output_text="",
            )
        return SimpleNamespace(
            output=[],
            output_text=json.dumps(
                {
                    "answer": "Tyrese Maxey resolves to a qualified 2025-26 player.",
                    "assumptions": ["Qualified players have at least 5 games."],
                    "tables": [],
                    "charts": [],
                    "metric_definitions": [
                        {
                            "key": "pts",
                            "label": "PTS",
                            "definition": "Points per game.",
                        }
                    ],
                    "followups": ["Show Maxey's points trend."],
                }
            ),
        )


class FakeOpenAIClient:
    def __init__(self) -> None:
        self.responses = FakeOpenAIResponses()


class FakeOpenAIDateRangeResponses:
    def __init__(self) -> None:
        self.calls = 0
        self.first_tools: list[dict] = []

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            self.first_tools = kwargs["tools"]
            return SimpleNamespace(
                output=[
                    SimpleNamespace(
                        type="function_call",
                        name="get_player_game_log",
                        arguments=json.dumps(
                            {
                                "player_id": 7,
                                "metrics": ["points"],
                                "limit": 82,
                                "start_date": "2026-02-02",
                                "end_date": "2026-02-03",
                            }
                        ),
                        call_id="call_1",
                    )
                ],
                output_text="",
            )
        return SimpleNamespace(
            output=[],
            output_text=json.dumps(
                {
                    "answer": "Date range filtered game log returned.",
                    "assumptions": [],
                    "tables": [],
                    "charts": [],
                    "metric_definitions": [],
                    "followups": [],
                }
            ),
        )


class FakeOpenAIDateRangeClient:
    def __init__(self) -> None:
        self.responses = FakeOpenAIDateRangeResponses()


def build_client(
    repo: WarehouseRepository | None = None,
    *,
    settings: Settings | None = None,
    agent_client: object | None = None,
) -> TestClient:
    app.dependency_overrides.clear()
    app.dependency_overrides[get_repository] = lambda: repo or FakeRepository()
    if settings is not None:
        app.dependency_overrides[get_settings] = lambda: settings
    if agent_client is not None:
        app.dependency_overrides[get_agent_client] = lambda: agent_client
    return TestClient(app)


def test_home_page_smoke() -> None:
    client = build_client()
    response = client.get("/")

    assert response.status_code == 200
    assert "Stats Dashboard" in response.text
    assert "Signal Board" in response.text
    assert "Choose Day" in response.text
    assert "Wed Feb 11" in response.text
    assert "https://cdn.nba.com/headshots/nba/latest/1040x760/7.png" in response.text
    assert "PTS +6.4" in response.text
    assert "Strengths PTS, AST, 3PM" in response.text
    assert "Strengths TOV" not in response.text
    assert "Tracked Players" not in response.text
    assert "/analysis" not in response.text
    assert "/recommendations" not in response.text
    assert "Risk " not in response.text


def test_home_page_shows_stale_notice() -> None:
    client = build_client(StaleRepository())
    response = client.get("/")

    assert response.status_code == 200
    assert "Use the board with caution." not in response.text
    assert "Last refresh" in response.text
    assert 'title="2026-02-09T12:00:00+00:00"' in response.text


def test_home_page_missing_opportunity_state() -> None:
    client = build_client(MissingOpportunityRepository())
    response = client.get("/")

    assert response.status_code == 200
    assert "Opportunity context is unavailable." in response.text


def test_home_page_respects_selected_day() -> None:
    client = build_client()
    response = client.get("/?as_of_date=2026-02-09")

    assert response.status_code == 200
    assert "Mon Feb 09" in response.text


def test_analysis_page_removed() -> None:
    client = build_client()
    response = client.get("/analysis")

    assert response.status_code == 404


def test_recommendations_page_removed() -> None:
    client = build_client()
    response = client.get("/recommendations")

    assert response.status_code == 404


def test_ask_page_smoke() -> None:
    client = build_client()
    response = client.get("/ask")

    assert response.status_code == 200
    assert "Ask NBA Stats" in response.text
    assert "/static/agent.js" in response.text


def test_api_agent_ask_returns_disabled_without_openai_key() -> None:
    client = build_client(settings=_test_settings(openai_api_key=None))
    response = client.post(
        "/api/agent/ask",
        json={"question": "How is Tyrese Maxey trending?"},
    )

    assert response.status_code == 503
    assert "OPENAI_API_KEY" in response.json()["detail"]


def test_api_agent_ask_runs_mocked_openai_tool_loop() -> None:
    fake_openai = FakeOpenAIClient()
    client = build_client(
        settings=_test_settings(openai_api_key="test-key"),
        agent_client=fake_openai,
    )
    response = client.post(
        "/api/agent/ask",
        json={"question": "How is Tyrese Maxey trending?"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["season"] == "2025-26"
    assert payload["answer"].startswith("Tyrese Maxey")
    assert payload["tool_calls"][0] == {"name": "resolve_player", "status": "ok"}
    assert fake_openai.responses.calls == 2


def test_api_agent_ask_accepts_date_range_tool_args() -> None:
    fake_openai = FakeOpenAIDateRangeClient()
    client = build_client(
        settings=_test_settings(openai_api_key="test-key"),
        agent_client=fake_openai,
    )
    response = client.post(
        "/api/agent/ask",
        json={"question": "Show Tyrese Maxey points from 2026-02-02 to 2026-02-03."},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tool_calls"][0] == {"name": "get_player_game_log", "status": "ok"}
    game_log_schema = next(
        tool
        for tool in fake_openai.responses.first_tools
        if tool["name"] == "get_player_game_log"
    )
    properties = game_log_schema["parameters"]["properties"]
    assert properties["start_date"]["type"] == ["string", "null"]
    assert properties["end_date"]["type"] == ["string", "null"]


def test_api_agent_ask_rate_limits_repeated_public_calls() -> None:
    fake_openai = FakeOpenAIClient()
    client = build_client(
        settings=_test_settings(
            openai_api_key="test-key",
            agent_rate_limit_per_minute=1,
        ),
        agent_client=fake_openai,
    )
    headers = {"X-Forwarded-For": "198.51.100.23"}

    first = client.post(
        "/api/agent/ask",
        json={"question": "How is Tyrese Maxey trending?"},
        headers=headers,
    )
    second = client.post(
        "/api/agent/ask",
        json={"question": "How is Tyrese Maxey trending?"},
        headers=headers,
    )

    assert first.status_code == 200
    assert second.status_code == 429
    assert "rate limit" in second.json()["detail"]
    assert fake_openai.responses.calls == 2


def test_player_page_smoke() -> None:
    client = build_client()
    response = client.get("/players/7")

    assert response.status_code == 200
    assert "Why This Player Matters" in response.text
    assert "Archetype" in response.text
    assert "Similar Players" in response.text
    assert "Compare player" in response.text
    assert "Find qualified player" in response.text
    assert "Performance" in response.text
    assert "Game Log" in response.text
    assert "Recent Windows" in response.text
    assert "League Percentiles" in response.text
    assert "Ball Security" in response.text
    assert "Box Score Index" in response.text
    assert "Primary Creator" in response.text
    assert "Jalen Brunson" in response.text
    assert "https://cdn.nba.com/headshots/nba/latest/1040x760/7.png" in response.text


def test_player_page_unavailable_state_smoke() -> None:
    client = build_client()
    response = client.get("/players/9")

    assert response.status_code == 200
    assert "This player is not currently ranked." in response.text


def test_compare_page_with_only_player_a() -> None:
    client = build_client()
    response = client.get("/compare?player_a_id=7")

    assert response.status_code == 200
    assert "Build Comparison" in response.text
    assert "Pick a second player to finish the comparison." in response.text


def test_compare_page_full_surface() -> None:
    client = build_client()
    response = client.get(
        "/compare?player_a_id=7&player_b_id=9&window=last_7&focus=scoring"
    )

    assert response.status_code == 200
    assert "Comparison Surface" in response.text
    assert "Focus Scoring" in response.text
    assert "Mikal Bridges" in response.text
    assert "Limited comparison data." in response.text
    assert "Stat-profile similarity." in response.text
    assert "Box Score Index" in response.text
    assert "Last 7" in response.text
    assert "https://cdn.nba.com/headshots/nba/latest/1040x760/7.png" in response.text


def test_compare_page_duplicate_validation() -> None:
    client = build_client()
    response = client.get("/compare?player_a_id=7&player_b_id=7")

    assert response.status_code == 200
    assert "Compare players must be different." in response.text


def test_api_health_smoke() -> None:
    client = build_client()
    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "fresh"
    assert payload["last_successful_finished_at_utc"] == "2026-02-11T01:15:00+00:00"
    assert "latest_successful_run" not in payload


def test_api_analysis_latest_includes_structured_sections() -> None:
    client = build_client()
    response = client.get("/api/analysis/latest")

    assert response.status_code == 200
    payload = response.json()
    assert payload["season"] == "2025-26"
    assert payload["item"]["trend_player"] == "Tyrese Maxey"
    assert (
        payload["item"]["score_contribution"]["player_points_share_of_team"] == 0.2768
    )
    assert payload["item"]["score_contribution"]["team_pts_qtr4"] == 30
    assert payload["item"]["player_context"]["position"] == "G"
    assert payload["item"]["player_context"]["roster_status"] is True


def test_api_player_search_rejects_blank_query() -> None:
    client = build_client()
    response = client.get("/api/players/search?q=%20%20%20")

    assert response.status_code == 400
    assert response.json()["detail"] == "Search query must not be blank"


def test_api_player_search_returns_qualified_player_metadata() -> None:
    client = build_client()
    response = client.get("/api/players/search?q=maxey")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["player_name"] == "Tyrese Maxey"
    assert item["is_qualified"] is True
    assert item["games_sampled"] == 12
    assert item["headshot_url"].endswith("/7.png")


def test_api_player_detail_available_player() -> None:
    client = build_client()
    response = client.get("/api/players/7")

    assert response.status_code == 200
    payload = response.json()
    assert payload["item"]["availability_state"] == "fresh"
    assert payload["item"]["player"]["overall_rank"] == 12
    assert payload["item"]["sample"]["games_sampled"] == 12
    assert payload["item"]["stat_percentiles"][0]["label"] == "PTS"
    assert payload["item"]["chart_baselines"]["pts"]["value"] == 12.4
    assert payload["item"]["game_log"]["games_returned"] == 5
    assert payload["item"]["player"]["headshot_url"].endswith("/7.png")
    assert payload["item"]["archetype"]["archetype_label"] == "Primary Creator"
    assert payload["item"]["similar_players"][0]["player_name"] == "Jalen Brunson"


def test_api_player_detail_unavailable_known_player() -> None:
    client = build_client()
    response = client.get("/api/players/9")

    assert response.status_code == 200
    payload = response.json()
    assert payload["item"]["availability_state"] == "unavailable"
    assert payload["item"]["availability_reason"] == "Not currently ranked"


def test_api_player_detail_404() -> None:
    client = build_client()
    response = client.get("/api/players/999")

    assert response.status_code == 404


def test_api_compare_smoke() -> None:
    client = build_client()
    response = client.get(
        "/api/compare?player_a_id=7&player_b_id=9&window=last_7&focus=scoring"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["window"] == "last_7"
    assert payload["focus"] == "scoring"
    assert payload["similarity"]["state"] == "fresh"
    assert payload["comparison"]["player_a"]["player_name"] == "Tyrese Maxey"
    assert payload["comparison"]["player_a"]["metric_rows"][1]["label"] == "PTS"
    assert payload["comparison"]["player_b"]["state"] == "unavailable"


def test_api_compare_rejects_duplicate_selection() -> None:
    client = build_client()
    response = client.get("/api/compare?player_a_id=7&player_b_id=7")

    assert response.status_code == 400
    assert response.json()["detail"] == "Compare players must be different"


def test_compare_page_logs_degraded_compare_side(caplog) -> None:
    client = build_client()

    with caplog.at_level("INFO", logger=LOGGER_NAME):
        response = client.get("/compare?player_a_id=7&player_b_id=9&window=last_7")

    assert response.status_code == 200
    events = [
        json.loads(record.message)
        for record in caplog.records
        if "panel_degraded" in record.message
    ]
    assert any(
        event["panel"] == "compare_side"
        and event["reason"] == "player_not_ranked"
        and event["slot"] == "player_b"
        for event in events
    )


def test_player_page_logs_degraded_panel_state(caplog) -> None:
    client = build_client(MissingOpportunityRepository())

    with caplog.at_level("INFO", logger=LOGGER_NAME):
        response = client.get("/players/7")

    assert response.status_code == 200
    events = [
        json.loads(record.message)
        for record in caplog.records
        if "panel_degraded" in record.message
    ]
    assert any(
        event["panel"] == "opportunity"
        and event["reason"] == "missing_schedule_context"
        and event["player_id"] == 7
        for event in events
    )


def test_home_page_logs_stale_freshness(caplog) -> None:
    client = build_client(StaleRepository())

    with caplog.at_level("INFO", logger=LOGGER_NAME):
        response = client.get("/")

    assert response.status_code == 200
    events = [
        json.loads(record.message)
        for record in caplog.records
        if "panel_degraded" in record.message
    ]
    assert any(
        event["panel"] == "freshness_banner"
        and event["reason"] == "stale_freshness"
        and event["surface"] == "dashboard"
        and event["as_of_date"] == "2026-02-11"
        for event in events
    )


def test_api_player_game_log_success() -> None:
    client = build_client()
    response = client.get("/api/players/7/game-log")

    assert response.status_code == 200
    payload = response.json()
    assert payload["item"]["player_id"] == 7
    assert payload["item"]["player_name"] == "Tyrese Maxey"
    assert len(payload["item"]["games"]) > 0
    assert "pts" in payload["item"]["games"][0]


def test_api_player_game_log_with_limit() -> None:
    client = build_client()
    response = client.get("/api/players/7/game-log?limit=3")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["item"]["games"]) == 3


def test_api_player_game_log_with_date_range() -> None:
    client = build_client()
    response = client.get(
        "/api/players/7/game-log?start_date=2026-02-02&end_date=2026-02-03"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["item"]["date_range"] == {
        "start_date": "2026-02-02",
        "end_date": "2026-02-03",
    }
    assert [game["game_date"] for game in payload["item"]["games"]] == [
        "2026-02-02",
        "2026-02-03",
    ]


def test_api_player_game_log_rejects_reversed_date_range() -> None:
    client = build_client()
    response = client.get(
        "/api/players/7/game-log?start_date=2026-02-03&end_date=2026-02-02"
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "start_date must be on or before end_date"


def test_api_player_game_log_404() -> None:
    client = build_client()
    response = client.get("/api/players/999/game-log")

    assert response.status_code == 404


def test_performance_page_smoke() -> None:
    client = build_client()
    response = client.get("/performance")

    assert response.status_code == 200
    assert "Game Performance" in response.text
    assert "/static/performance.js" in response.text
    assert "performance.js?v=20260523-performance-polish" in response.text
    assert "performance-date-select" in response.text
    assert "Player View" in response.text


def test_api_performance_dates() -> None:
    client = build_client()
    response = client.get("/api/performance/dates")

    assert response.status_code == 200
    payload = response.json()
    assert payload["season"] == "2025-26"
    assert payload["items"][0]["value"] == "2026-02-10"


def test_api_performance_games_filters_by_date() -> None:
    client = build_client()
    response = client.get("/api/performance/games?game_date=2026-02-10")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["game_id"] == "002250010"
    assert item["matchup"] == "NYK @ PHI"
    assert item["players_played"] == 20


def test_api_performance_players_returns_baseline_deltas() -> None:
    client = build_client()
    response = client.get(
        "/api/performance/players?game_date=2026-02-10&game_id=002250010"
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["player_name"] == "Tyrese Maxey"
    assert item["performance_status"] == "above"
    assert item["metrics"][0]["label"] == "PTS"
    assert item["metrics"][0]["delta"] == 5.2
    assert "trend_30d" not in item


def test_api_performance_player_detail_returns_percentile_ranges() -> None:
    client = build_client()
    response = client.get("/api/performance/players/7?game_id=002250010")

    assert response.status_code == 200
    item = response.json()["item"]
    assert item["player_name"] == "Tyrese Maxey"
    assert item["metrics"][0]["percentile"] == 82.0
    assert item["metrics"][0]["range"]["p25"] == 22
    assert item["trend_30d"]["window_days"] == 30
    assert item["trend_30d"]["stats"][0]["season_average"] == 25.8
    assert item["trend_30d"]["points"][-1]["game_id"] == "002250010"


def test_visualize_page_smoke() -> None:
    client = build_client()
    response = client.get("/visualize")

    assert response.status_code == 200
    assert "Player Stats Explorer" in response.text
    assert "chart.js" in response.text
    assert "visualize.js" in response.text


def test_visualize_page_with_player_id() -> None:
    client = build_client()
    response = client.get("/visualize?player_id=7")

    assert response.status_code == 200
    assert "Player Stats Explorer" in response.text
    assert "__vizBootstrap" in response.text


def test_api_similarity_map_returns_players_and_archetypes() -> None:
    client = build_client()
    response = client.get("/api/similarity-map")

    assert response.status_code == 200
    payload = response.json()
    assert payload["season"] == "2025-26"
    assert payload["players"][0]["player_name"] == "Alpha Guard"
    assert payload["players"][0]["x"] == 0.12
    assert payload["archetypes"] == [{"archetype_label": "Scoring Guard", "count": 1}]
    assert payload["axes"][0]["key"] == "proj_x"
    assert payload["axes"][0]["drivers"] == ["scoring volume", "usage"]


def test_similarity_map_page_smoke() -> None:
    client = build_client()
    response = client.get("/similarity-map")

    assert response.status_code == 200
    assert "Player Similarity Map" in response.text
    assert "/static/similarity_map.js" in response.text
    assert "plotly-gl3d" in response.text
    assert 'id="map-axes-note"' in response.text


def test_api_similarity_map_neighbors_returns_ranked_matches() -> None:
    client = build_client()
    response = client.get("/api/similarity-map/neighbors/1?limit=5")

    assert response.status_code == 200
    payload = response.json()
    assert payload["player_id"] == 1
    assert payload["neighbors"][0]["player_name"] == "Beta Guard"
    assert payload["neighbors"][0]["similarity_score"] == 0.91


def test_api_similarity_map_neighbors_rejects_out_of_range_limit() -> None:
    client = build_client()
    response = client.get("/api/similarity-map/neighbors/1?limit=99")

    assert response.status_code == 422


def test_similarity_map_page_has_search_and_panel() -> None:
    client = build_client()
    response = client.get("/similarity-map")

    assert response.status_code == 200
    assert 'id="map-search-input"' in response.text
    assert 'id="map-panel"' in response.text
    assert "true nearest matches" in response.text
