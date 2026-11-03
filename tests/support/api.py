"""Synthetic api factories; source and policy profiles remain explicit."""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.config import Settings, get_settings
from app.main import app, get_agent_client, get_repository
from app.repository import WarehouseRepository
from fastapi.testclient import TestClient


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
                    "active_model_key": "gmm",
                    "recommended_model_key": "gmm",
                },
                "similarity_models": [
                    {
                        "model_key": "kmeans",
                        "model_label": "KMeans baseline",
                        "description": "Fast deterministic baseline.",
                        "archetype_label": "Primary Creator",
                        "cluster_confidence": 0.82,
                        "top_traits": ["playmaking", "usage share"],
                        "is_recommended": False,
                        "is_baseline": True,
                    },
                    {
                        "model_key": "gmm",
                        "model_label": "Gaussian mixture",
                        "description": "Soft clustering for hybrid profiles.",
                        "archetype_label": "Primary Creator",
                        "cluster_confidence": 0.92,
                        "top_traits": ["playmaking", "usage share"],
                        "is_recommended": True,
                        "is_baseline": False,
                    },
                ],
                "similarity_model_evaluation": {
                    "recommended_model_key": "gmm",
                    "baseline_model_key": "kmeans",
                    "models": [],
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
                    "active_model_key": None,
                    "recommended_model_key": None,
                },
                "similarity_models": [],
                "similarity_model_evaluation": {
                    "recommended_model_key": None,
                    "models": [],
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

    def get_recent_performance_initial(
        self,
        *,
        game_date: str | None = None,
        game_id: str | None = None,
        limit: int = 240,
    ) -> dict:
        dates = self.get_recent_performance_dates()
        selected_date = game_date or (dates[0]["value"] if dates else None)
        if not selected_date:
            return {
                "season": "2025-26",
                "dates": dates,
                "selected_date": None,
                "selected_game_id": game_id,
                "games": [],
                "players": [],
            }
        return {
            "season": "2025-26",
            "dates": dates,
            "selected_date": selected_date,
            "selected_game_id": game_id,
            "games": self.get_recent_performance_games(game_date=selected_date),
            "players": self.get_recent_performance_players(
                game_date=selected_date,
                game_id=game_id,
                limit=limit,
            ),
        }

    def _performance_row(self) -> dict:
        return {
            "game_id": "002250010",
            "game_date": "2026-02-10",
            "season_type": "Playoffs",
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
                {
                    "key": "min",
                    "label": "MIN",
                    "value": 36.0,
                    "season_average": 34.8,
                    "delta": 1.2,
                    "delta_pct": 3.4,
                    "status": "above",
                    "percentile": 70.0,
                    "range": {
                        "p10": 28.0,
                        "p25": 32.0,
                        "median": 35.0,
                        "p75": 37.0,
                        "p90": 39.0,
                    },
                },
                {
                    "key": "fg_pct",
                    "label": "FG%",
                    "value": 50.0,
                    "season_average": 46.8,
                    "delta": 3.2,
                    "delta_pct": 6.8,
                    "status": "above",
                    "format": "percent",
                    "percentile": 72.0,
                    "range": {
                        "p10": 39.0,
                        "p25": 43.0,
                        "median": 47.0,
                        "p75": 52.0,
                        "p90": 58.0,
                    },
                },
                {
                    "key": "ft_pct",
                    "label": "FT%",
                    "value": 80.0,
                    "season_average": 84.0,
                    "delta": -4.0,
                    "delta_pct": -4.8,
                    "status": "below",
                    "format": "percent",
                    "percentile": 45.0,
                    "range": {
                        "p10": 65.0,
                        "p25": 75.0,
                        "median": 84.0,
                        "p75": 90.0,
                        "p90": 96.0,
                    },
                },
                {
                    "key": "fg3m",
                    "label": "3PM",
                    "value": 3,
                    "season_average": 2.4,
                    "delta": 0.6,
                    "delta_pct": 25.0,
                    "status": "above",
                    "percentile": 69.0,
                    "range": {
                        "p10": 0,
                        "p25": 1,
                        "median": 2,
                        "p75": 4,
                        "p90": 5,
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
                {"key": "min", "label": "MIN", "season_average": 34.8},
                {
                    "key": "fg_pct",
                    "label": "FG%",
                    "season_average": 46.8,
                    "format": "percent",
                },
                {
                    "key": "ft_pct",
                    "label": "FT%",
                    "season_average": 84.0,
                    "format": "percent",
                },
                {"key": "fg3m", "label": "3PM", "season_average": 2.4},
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
                    "min": 35.0,
                    "fg_pct": 48.0,
                    "ft_pct": 75.0,
                    "fg3m": 2,
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
                    "min": 36.0,
                    "fg_pct": 50.0,
                    "ft_pct": 80.0,
                    "fg3m": 3,
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
            "regular_season": "Regular Season",
            "regular_season_first_half": "First Half Regular Season",
            "regular_season_second_half": "Second Half Regular Season",
            "playoffs": "Playoffs",
        }
        expected_games = {
            "last_3": 3,
            "last_5": 5,
            "last_7": 7,
            "prior_5": 5,
            "last_10": 10,
            "regular_season": None,
            "regular_season_first_half": None,
            "regular_season_second_half": None,
            "playoffs": None,
        }
        games_in_window = {
            "last_3": 3,
            "last_5": 5,
            "last_7": 7,
            "prior_5": 5,
            "last_10": 10,
            "regular_season": 82,
            "regular_season_first_half": 41,
            "regular_season_second_half": 41,
            "playoffs": 12,
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
                    "games_in_window": games_in_window[window],
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
                    "active_model_key": "kmeans",
                    "recommended_model_key": "kmeans",
                    "model_assignments": [
                        {
                            "model_key": "kmeans",
                            "model_label": "KMeans baseline",
                            "description": "Fast deterministic baseline.",
                            "archetype_label": "Scoring Guard",
                            "cluster_confidence": 0.82,
                            "top_traits": ["scoring volume"],
                            "is_recommended": True,
                            "is_baseline": True,
                        }
                    ],
                    "games_sampled": 20,
                    "sample_status": "ready",
                    "x": 0.12,
                    "y": -0.34,
                    "z": 0.05,
                }
            ],
            "archetypes": [{"archetype_label": "Scoring Guard", "count": 1}],
            "models": [
                {
                    "model_key": "kmeans",
                    "model_label": "KMeans baseline",
                    "description": "Fast deterministic baseline.",
                    "is_recommended": True,
                    "is_baseline": True,
                }
            ],
            "model_evaluation": {
                "recommended_model_key": "kmeans",
                "baseline_model_key": "kmeans",
                "models": [
                    {
                        "model_key": "kmeans",
                        "model_label": "KMeans baseline",
                        "score": 0.78,
                    }
                ],
            },
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
            "season_coverage": {
                "season": "2025-26",
                "first_game_date": "2025-10-21",
                "latest_game_date": "2026-06-14",
                "game_count": 1230,
                "player_game_rows": 31200,
                "season_types": ["Regular Season", "Playoffs"],
                "is_full_season": True,
            },
        }


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
    def __init__(
        self,
        answer: str = "Tyrese Maxey resolves to a qualified 2025-26 player.",
    ) -> None:
        self.calls = 0
        self.kwargs: list[dict] = []
        self.answer = answer

    def create(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
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
                    "answer": self.answer,
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
    def __init__(
        self,
        answer: str = "Tyrese Maxey resolves to a qualified 2025-26 player.",
    ) -> None:
        self.responses = FakeOpenAIResponses(answer=answer)


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
    if settings and settings.agent_history_enabled:
        return TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))
    return TestClient(app)
