"""Synthetic agent factories; source and policy profiles remain explicit."""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.config import Settings


def _settings(**overrides: object) -> Settings:
    values = {
        "project_id": "local-project",
        "gold_dataset": "nba_gold",
        "metadata_dataset": "nba_metadata",
        "freshness_threshold_hours": 36,
        "max_search_results": 12,
        "openai_api_key": "test-key",
        "openai_agent_model": "gpt-5.4-mini",
        "openai_agent_enabled": True,
        "agent_max_tool_calls": 6,
        "agent_rate_limit_per_minute": 0,
    }
    values.update(overrides)
    return Settings(**values)


class AgentServiceFakeRepository:
    def search_players(self, query: str, limit: int = 10) -> list[dict]:
        return [
            {
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "latest_team_abbr": "PHI",
                "latest_game_date": "2026-02-10",
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
            },
            "trends": [{"stat": "PTS", "label": "PTS", "delta": 4.2}],
            "game_log": {
                "games": [
                    {
                        "game_date": "2026-02-10",
                        "matchup": "PHI vs. NYK",
                        "wl": "W",
                        "pts": "30",
                        "reb": "4",
                        "ast": "8",
                        "stl": "1",
                        "blk": "0",
                        "tov": "2",
                    }
                ]
            },
            "chart_baselines": {},
            "similar_players": [{"player_id": 11, "player_name": "Jalen Brunson"}],
            "stat_percentiles": [
                {"key": "pts", "label": "PTS", "average": 25.0, "percentile": 91.0}
            ],
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
                "game_date": "2026-02-10",
                "matchup": "PHI vs. NYK",
                "wl": "W",
                "team_abbr": "PHI",
                "opponent_abbr": "NYK",
                "pts": "30",
                "reb": "4",
                "ast": "8",
                "stl": "1",
                "blk": "0",
                "tov": "2",
            },
            {
                "game_date": "2026-02-08",
                "matchup": "PHI @ BOS",
                "wl": "L",
                "team_abbr": "PHI",
                "opponent_abbr": "BOS",
                "pts": "18",
                "reb": "3",
                "ast": "5",
                "stl": "0",
                "blk": "0",
                "tov": "4",
            },
        ]
        return {
            "player_id": 7,
            "player_name": "Tyrese Maxey",
            "season": "2025-26",
            "games": games[:limit],
        }

    def get_metric_leaders(self, metric: str, limit: int = 10) -> list[dict]:
        return [
            {
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "metric_key": metric,
                "metric_value": 30.0,
            }
        ][:limit]

    def get_player_metric_percentile(
        self, player_id: int, metric: str, min_games: int = 5
    ) -> dict | None:
        if player_id != 7:
            return None
        return {
            "player_name": "Tyrese Maxey",
            "metric_key": metric,
            "percentile": 91.0,
            "in_requested_cohort": True,
        }


class SequenceResponses:
    def __init__(self, tool_calls: list[SimpleNamespace]) -> None:
        self.calls = 0
        self.kwargs: list[dict] = []
        self.tool_calls = tool_calls

    def create(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        if self.calls == 1 and self.tool_calls:
            return SimpleNamespace(output=self.tool_calls, output_text="")
        return SimpleNamespace(
            output=[],
            output_text=json.dumps(
                {
                    "answer": "Answered from mocked tools.",
                    "assumptions": [],
                    "tables": [],
                    "charts": [],
                    "metric_definitions": [],
                    "followups": [],
                }
            ),
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
        )


class SequenceClient:
    def __init__(self, tool_calls: list[SimpleNamespace]) -> None:
        self.responses = SequenceResponses(tool_calls)
