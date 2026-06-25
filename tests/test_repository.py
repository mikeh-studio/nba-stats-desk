from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from google.api_core.exceptions import BadRequest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.repository import (
    SIMILARITY_FEATURE_COLUMNS,
    BigQueryWarehouseRepository,
    _weighted_similarity_vector,
    build_analysis_payload,
    build_freshness_payload,
    build_season_coverage_payload,
)


def _build_repository() -> BigQueryWarehouseRepository:
    return BigQueryWarehouseRepository(
        Settings(
            project_id="local-project",
            gold_dataset="nba_gold",
            metadata_dataset="nba_metadata",
            freshness_threshold_hours=36,
            max_search_results=12,
        ),
        client=object(),
    )


def test_weighted_similarity_vector_l2_normalizes_components() -> None:
    row = {f"norm_{feature_name}": 0.0 for feature_name in SIMILARITY_FEATURE_COLUMNS}
    row["norm_season_avg_pts"] = 3.0
    row["norm_season_avg_reb"] = 4.0

    vector = _weighted_similarity_vector(row)

    squared_norm = sum(component**2 for component in vector.values())
    assert round(squared_norm, 6) == 1.0
    assert vector["season_avg_pts"] > 0
    assert vector["season_avg_reb"] > 0


def test_build_freshness_payload_fresh() -> None:
    payload = build_freshness_payload(
        {"finished_at_utc": "2026-02-11T01:15:00+00:00"},
        now=datetime(2026, 2, 11, 2, 0, tzinfo=UTC),
        freshness_threshold_hours=36,
    )

    assert payload["status"] == "fresh"
    assert payload["is_fresh"] is True
    assert payload["age_hours"] == 0.8


def test_build_freshness_payload_stale() -> None:
    payload = build_freshness_payload(
        {"finished_at_utc": "2026-02-09T01:15:00+00:00"},
        now=datetime(2026, 2, 11, 2, 0, tzinfo=UTC),
        freshness_threshold_hours=36,
    )

    assert payload["status"] == "stale"
    assert payload["is_fresh"] is False


def test_build_freshness_payload_missing() -> None:
    payload = build_freshness_payload(
        None,
        now=datetime(2026, 2, 11, 2, 0, tzinfo=UTC),
        freshness_threshold_hours=36,
    )

    assert payload["status"] == "missing"
    assert payload["last_successful_finished_at_utc"] is None


def test_build_freshness_payload_missing_finished_at_is_unavailable() -> None:
    payload = build_freshness_payload(
        {"finished_at_utc": None},
        now=datetime(2026, 2, 11, 2, 0, tzinfo=UTC),
        freshness_threshold_hours=36,
    )

    assert payload["status"] == "unavailable"
    assert payload["is_fresh"] is False


def test_build_season_coverage_payload_marks_full_season() -> None:
    payload = build_season_coverage_payload(
        {
            "season": "2025-26",
            "first_game_date": "2025-10-21",
            "latest_game_date": "2026-06-14",
            "game_count": "1230",
            "player_game_rows": "31200",
            "has_regular_season": "true",
            "has_playoffs": "true",
        }
    )

    assert payload == {
        "season": "2025-26",
        "first_game_date": "2025-10-21",
        "latest_game_date": "2026-06-14",
        "game_count": 1230,
        "player_game_rows": 31200,
        "season_types": ["Regular Season", "Playoffs"],
        "is_full_season": True,
    }


def test_build_season_coverage_payload_requires_games() -> None:
    assert build_season_coverage_payload({"game_count": "0"}) is None


def test_build_analysis_payload_nests_structured_sections() -> None:
    payload = build_analysis_payload(
        {
            "snapshot_id": "202526_20260211",
            "season": "2025-26",
            "contribution_player_id": "7",
            "contribution_player_name": "Tyrese Maxey",
            "contribution_team_abbr": "PHI",
            "contribution_player_points_share_of_team": "0.2768",
            "contribution_team_pts_qtr4": "30",
            "context_player_id": "7",
            "context_player_name": "Tyrese Maxey",
            "context_position": "G",
            "context_roster_status": "true",
            "context_weight": "200",
        }
    )

    assert payload is not None
    assert payload["score_contribution"]["player_id"] == 7
    assert payload["score_contribution"]["player_points_share_of_team"] == 0.2768
    assert payload["score_contribution"]["team_pts_qtr4"] == 30
    assert payload["player_context"]["position"] == "G"
    assert payload["player_context"]["roster_status"] is True
    assert payload["player_context"]["weight"] == 200


def test_fetch_similarity_anchor_returns_none_on_bigquery_error(monkeypatch) -> None:
    repo = _build_repository()

    def fake_query(*_args, **_kwargs):
        raise BadRequest("similarity table missing")

    monkeypatch.setattr(repo, "_query", fake_query)

    assert repo._fetch_similarity_anchor(7) is None


def test_search_players_uses_agent_dataset_index(monkeypatch) -> None:
    repo = _build_repository()

    def fake_query(sql, *_args, **_kwargs):
        assert "nba_agent.agent_player_search" in sql
        return [
            {
                "player_id": "7",
                "player_name": "Tyrese Maxey",
                "latest_season": "2025-26",
                "latest_team_abbr": "PHI",
                "latest_game_date": "2026-02-11",
                "games_sampled": "12",
                "qualification_games": "5",
                "is_qualified": "true",
                "sample_status": "ready",
                "overall_rank": "12",
                "recommendation_score": "8.4",
            }
        ]

    monkeypatch.setattr(repo, "_query", fake_query)

    rows = repo.search_players("Maxey")

    assert rows[0]["player_id"] == 7
    assert rows[0]["latest_team_abbr"] == "PHI"
    assert rows[0]["overall_rank"] == 12


def test_fetch_player_identity_uses_agent_dataset_index(monkeypatch) -> None:
    repo = _build_repository()

    def fake_query(sql, *_args, **_kwargs):
        assert "nba_agent.agent_player_search" in sql
        return [
            {
                "player_id": "7",
                "player_name": "Tyrese Maxey",
                "latest_season": "2025-26",
                "latest_team_abbr": "PHI",
                "latest_game_date": "2026-02-11",
                "games_sampled": "12",
                "qualification_games": "5",
                "is_qualified": "true",
                "sample_status": "ready",
            }
        ]

    monkeypatch.setattr(repo, "_query", fake_query)

    identity = repo._fetch_player_identity(7)

    assert identity is not None
    assert identity["player_name"] == "Tyrese Maxey"


def test_search_players_falls_back_to_game_stats_when_index_missing(
    monkeypatch,
) -> None:
    repo = _build_repository()

    def fake_query(sql, *_args, **_kwargs):
        if "agent_player_search" in sql:
            raise BadRequest("agent search table missing")
        if "player_search_index" in sql:
            raise BadRequest("legacy search index missing")
        assert "fct_player_game_stats" in sql
        return [
            {
                "player_id": "2544",
                "player_name": "LeBron James",
                "latest_season": "2025-26",
                "latest_team_abbr": "LAL",
                "games_sampled": "60",
                "qualification_games": "5",
                "is_qualified": "true",
                "sample_status": "ready",
            }
        ]

    monkeypatch.setattr(repo, "_query", fake_query)

    rows = repo.search_players("LeBron")

    assert rows[0]["player_id"] == 2544
    assert rows[0]["is_qualified"] is True
    assert rows[0]["headshot_url"].endswith("/2544.png")


def test_fetch_player_identity_falls_back_to_game_stats_when_index_missing(
    monkeypatch,
) -> None:
    repo = _build_repository()

    def fake_query(sql, *_args, **_kwargs):
        if "agent_player_search" in sql:
            raise BadRequest("agent search table missing")
        if "player_search_index" in sql:
            raise BadRequest("legacy search index missing")
        assert "fct_player_game_stats" in sql
        return [
            {
                "player_id": "2544",
                "player_name": "LeBron James",
                "latest_season": "2025-26",
                "latest_team_abbr": "LAL",
                "games_sampled": "60",
                "qualification_games": "5",
                "is_qualified": "true",
                "sample_status": "ready",
            }
        ]

    monkeypatch.setattr(repo, "_query", fake_query)

    identity = repo._fetch_player_identity(2544)

    assert identity is not None
    assert identity["player_name"] == "LeBron James"


def test_get_similar_players_returns_unavailable_on_bigquery_error(
    monkeypatch,
) -> None:
    repo = _build_repository()

    def fake_query(*_args, **_kwargs):
        raise BadRequest("similarity table missing")

    monkeypatch.setattr(repo, "_query", fake_query)

    state, reason, players = repo._get_similar_players(
        7,
        anchor={
            "sample_status": "ready",
            "top_traits": "playmaking, usage share",
        },
    )

    assert state == "unavailable"
    assert reason == "Similarity profile is unavailable."
    assert players == []


def test_get_pair_similarity_returns_unavailable_when_similarity_query_fails(
    monkeypatch,
) -> None:
    repo = _build_repository()

    def fake_query(*_args, **_kwargs):
        raise BadRequest("similarity table missing")

    monkeypatch.setattr(repo, "_query", fake_query)

    payload = repo._get_pair_similarity(7, 11)

    assert payload["state"] == "unavailable"
    assert payload["score"] is None
    assert (
        payload["summary"]
        == "Similarity profile is unavailable for at least one player."
    )


def test_get_player_game_log_applies_date_range_filters(monkeypatch) -> None:
    repo = _build_repository()

    def fake_query(sql, params, *_args, **_kwargs):
        if "agent_player_search" in sql:
            return [
                {
                    "player_id": "7",
                    "player_name": "Tyrese Maxey",
                    "latest_season": "2025-26",
                    "latest_team_abbr": "PHI",
                    "games_sampled": "12",
                    "qualification_games": "5",
                    "is_qualified": "true",
                    "sample_status": "ready",
                }
            ]
        assert "fct_player_game_stats" in sql
        assert "game_date >= @start_date" in sql
        assert "game_date <= @end_date" in sql
        param_names = {param.name for param in params}
        assert {"season", "player_id", "limit", "start_date", "end_date"} <= param_names
        return [
            {
                "game_id": "002250001",
                "season": "2025-26",
                "game_date": "2026-02-03",
                "player_id": "7",
                "player_name": "Tyrese Maxey",
                "team_abbr": "PHI",
                "opponent_abbr": "BOS",
                "home_away": "away",
                "matchup": "PHI @ BOS",
                "wl": "W",
                "min": "36.0",
                "pts": "31",
                "reb": "4",
                "ast": "8",
                "stl": "2",
                "blk": "1",
                "tov": "3",
                "fg3m": "4",
                "fgm": "10",
                "fga": "20",
                "fg_pct": "0.500",
                "ftm": "7",
                "fta": "8",
                "ft_pct": "0.875",
                "fantasy_points_simple": "48.0",
            }
        ]

    monkeypatch.setattr(repo, "_query", fake_query)

    payload = repo.get_player_game_log(
        7,
        limit=20,
        start_date="2026-02-01",
        end_date="2026-02-05",
    )

    assert payload is not None
    assert payload["date_range"] == {
        "start_date": "2026-02-01",
        "end_date": "2026-02-05",
    }
    assert payload["games"][0]["game_date"] == "2026-02-03"


def test_get_recent_performance_dates_uses_playoff_scope(monkeypatch) -> None:
    repo = _build_repository()

    def fake_query(sql, params, *_args, **_kwargs):
        assert "season_type = 'Playoffs'" in sql
        assert "COALESCE(SAFE_CAST(stats.min AS FLOAT64), 0) >= 1" in sql
        assert "INTERVAL 6 DAY" not in sql
        assert "fct_player_game_stats" in sql
        assert {param.name for param in params} == {"season"}
        return [{"game_date": "2026-02-10"}]

    monkeypatch.setattr(repo, "_query", fake_query)

    rows = repo.get_recent_performance_dates()

    assert rows == [{"value": "2026-02-10", "label": "Tue Feb 10"}]


def test_get_recent_performance_initial_falls_back_to_serving_table_sql(
    monkeypatch,
) -> None:
    repo = _build_repository()
    calls: list[str] = []

    def fake_query(sql, params, *_args, **_kwargs):
        calls.append(sql)
        assert "recent_performance_workbench" in sql
        assert "TO_JSON_STRING(STRUCT(" in sql
        assert "ANY_VALUE(rows.game_matchup) AS matchup" in sql
        assert "selected_rows.game_id = @game_id" in sql
        assert "fg_pct" in sql
        assert "avg_fg_pct" in sql
        assert "UNION ALL" in sql
        param_names = {param.name for param in params}
        assert {"season", "game_date", "game_id", "limit"} == param_names
        return [
            {
                "section": "date",
                "sort_index": "1",
                "payload": json.dumps({"game_date": "2026-02-10"}),
            },
            {
                "section": "game",
                "sort_index": "1",
                "payload": json.dumps(
                    {
                        "game_id": "002250010",
                        "game_date": "2026-02-10",
                        "teams": "NYK / PHI",
                        "matchup": "NYK @ PHI",
                        "home_team_abbr": "PHI",
                        "away_team_abbr": "NYK",
                        "home_team_pts": "112",
                        "away_team_pts": "108",
                        "players_played": "20",
                    }
                ),
            },
            {
                "section": "player",
                "sort_index": "1",
                "payload": json.dumps(
                    {
                        "game_id": "002250010",
                        "game_date": "2026-02-10",
                        "season_type": "Playoffs",
                        "player_id": "7",
                        "player_name": "Tyrese Maxey",
                        "team_abbr": "PHI",
                        "opponent_abbr": "NYK",
                        "home_away": "home",
                        "matchup": "PHI vs. NYK",
                        "wl": "W",
                        "min": "36.0",
                        "pts": "31",
                        "reb": "5",
                        "ast": "7",
                        "stl": "1",
                        "blk": "0",
                        "fg_pct": "0.500",
                        "ft_pct": "0.800",
                        "fg3m": "3",
                        "games_sampled": "12",
                        "avg_pts": "25.8",
                        "avg_reb": "4.4",
                        "avg_ast": "6.7",
                        "avg_stl": "1.1",
                        "avg_blk": "0.2",
                        "avg_min": "34.8",
                        "avg_fg_pct": "0.468",
                        "avg_ft_pct": "0.840",
                        "avg_fg3m": "2.4",
                        "pts_delta": "5.2",
                        "reb_delta": "0.6",
                        "ast_delta": "0.3",
                        "stl_delta": "-0.1",
                        "blk_delta": "-0.2",
                        "min_delta": "1.2",
                        "fg_pct_delta": "0.032",
                        "ft_pct_delta": "-0.040",
                        "fg3m_delta": "0.6",
                        "performance_score": "2.4",
                        "performance_status": "above",
                        "above_count": "3",
                        "below_count": "2",
                    }
                ),
            },
        ]

    monkeypatch.setattr(repo, "_query", fake_query)

    payload = repo.get_recent_performance_initial(
        game_date="2026-02-10",
        game_id="002250010",
    )

    assert payload["selected_date"] == "2026-02-10"
    assert payload["selected_game_id"] == "002250010"
    assert payload["dates"][0] == {"value": "2026-02-10", "label": "Tue Feb 10"}
    assert payload["games"][0]["matchup"] == "NYK @ PHI"
    assert payload["games"][0]["home_team_pts"] == 112
    assert payload["players"][0]["player_id"] == 7
    assert payload["players"][0]["metrics"][0]["delta"] == 5.2
    metrics = {item["key"]: item for item in payload["players"][0]["metrics"]}
    assert metrics["fg_pct"]["value"] == 50.0
    assert metrics["fg_pct"]["delta"] == 3.2
    assert metrics["ft_pct"]["value"] == 80.0
    assert metrics["fg3m"]["value"] == 3.0
    assert len(calls) == 1


def test_get_recent_performance_initial_uses_table_rows_api(
    monkeypatch,
) -> None:
    repo = _build_repository()

    class FakeClient:
        def get_table(self, table_id):
            pytest.fail("list_rows should use selected_fields instead of get_table")

        def list_rows(self, table, selected_fields, max_results):
            assert table == "local-project.nba_gold.recent_performance_workbench"
            field_names = [field.name for field in selected_fields]
            assert field_names[:3] == ["season", "season_type", "game_id"]
            for field_name in [
                "fg_pct",
                "ft_pct",
                "fg3m",
                "avg_min",
                "avg_fg_pct",
                "avg_ft_pct",
                "avg_fg3m",
                "min_delta",
                "fg_pct_delta",
                "ft_pct_delta",
                "fg3m_delta",
            ]:
                assert field_name in field_names
            assert "pts_percentile" in field_names
            assert "pts_p25" in field_names
            assert "reb_percentile" in field_names
            assert "performance_score" in field_names
            assert "performance_status" in field_names
            assert "above_count" in field_names
            assert "below_count" in field_names
            assert "trend_points_json" in field_names
            assert max_results == 5000
            return [
                {
                    "season": "2025-26",
                    "season_type": "Playoffs",
                    "game_id": "002250010",
                    "game_date": "2026-02-10",
                    "teams": "NYK / PHI",
                    "game_matchup": "NYK @ PHI",
                    "home_team_abbr": "PHI",
                    "away_team_abbr": "NYK",
                    "home_team_pts": "112",
                    "away_team_pts": "108",
                    "players_played": "20",
                    "player_id": "8",
                    "player_name": "Aaron Example",
                    "team_abbr": "PHI",
                    "opponent_abbr": "NYK",
                    "home_away": "home",
                    "matchup": "PHI vs. NYK",
                    "wl": "W",
                    "min": "31.0",
                    "pts": "20",
                    "reb": "7",
                    "ast": "4",
                    "stl": "1",
                    "blk": "1",
                    "fg_pct": "0.520",
                    "ft_pct": "0.750",
                    "fg3m": "2",
                    "games_sampled": "12",
                    "avg_pts": "18.0",
                    "avg_reb": "5.0",
                    "avg_ast": "3.0",
                    "avg_stl": "0.8",
                    "avg_blk": "0.4",
                    "avg_min": "30.0",
                    "avg_fg_pct": "0.490",
                    "avg_ft_pct": "0.800",
                    "avg_fg3m": "1.4",
                    "pts_delta": "2.0",
                    "reb_delta": "2.0",
                    "ast_delta": "1.0",
                    "stl_delta": "0.2",
                    "blk_delta": "0.6",
                    "min_delta": "1.0",
                    "fg_pct_delta": "0.030",
                    "ft_pct_delta": "-0.050",
                    "fg3m_delta": "0.6",
                    "performance_score": "2.4",
                    "performance_status": "above",
                    "above_count": "3",
                    "below_count": "2",
                },
                {
                    "season": "2025-26",
                    "season_type": "Playoffs",
                    "game_id": "002250010",
                    "game_date": "2026-02-10",
                    "teams": "NYK / PHI",
                    "game_matchup": "NYK @ PHI",
                    "home_team_abbr": "PHI",
                    "away_team_abbr": "NYK",
                    "home_team_pts": "112",
                    "away_team_pts": "108",
                    "players_played": "20",
                    "player_id": "7",
                    "player_name": "Tyrese Maxey",
                    "team_abbr": "PHI",
                    "opponent_abbr": "NYK",
                    "home_away": "home",
                    "matchup": "PHI vs. NYK",
                    "wl": "W",
                    "min": "36.0",
                    "pts": "31",
                    "reb": "5",
                    "ast": "7",
                    "stl": "1",
                    "blk": "0",
                    "fg_pct": "0.500",
                    "ft_pct": "0.800",
                    "fg3m": "3",
                    "games_sampled": "12",
                    "avg_pts": "25.8",
                    "avg_reb": "4.4",
                    "avg_ast": "6.7",
                    "avg_stl": "1.1",
                    "avg_blk": "0.2",
                    "avg_min": "34.8",
                    "avg_fg_pct": "0.468",
                    "avg_ft_pct": "0.840",
                    "avg_fg3m": "2.4",
                    "pts_delta": "5.2",
                    "reb_delta": "0.6",
                    "ast_delta": "0.3",
                    "stl_delta": "-0.1",
                    "blk_delta": "-0.2",
                    "min_delta": "1.2",
                    "fg_pct_delta": "0.032",
                    "ft_pct_delta": "-0.040",
                    "fg3m_delta": "0.6",
                    "performance_score": "2.4",
                    "performance_status": "above",
                    "above_count": "3",
                    "below_count": "2",
                },
            ]

    repo.client = FakeClient()
    monkeypatch.setattr(
        repo,
        "_query",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(BadRequest("SQL unavailable")),
    )

    payload = repo.get_recent_performance_initial(game_date="2026-02-10")

    assert payload["dates"] == [{"value": "2026-02-10", "label": "Tue Feb 10"}]
    assert payload["games"][0]["matchup"] == "NYK @ PHI"
    assert [player["player_name"] for player in payload["players"]] == [
        "Aaron Example",
        "Tyrese Maxey",
    ]


def test_recent_performance_table_rows_cache_feeds_initial_and_detail(
    monkeypatch,
) -> None:
    repo = _build_repository()

    class FakeClient:
        calls = 0

        def list_rows(self, table, selected_fields, max_results):
            self.calls += 1
            assert table == "local-project.nba_gold.recent_performance_workbench"
            field_names = [field.name for field in selected_fields]
            assert "pts_percentile" in field_names
            assert "trend_points_json" in field_names
            assert max_results == 5000
            return [
                {
                    "season": "2025-26",
                    "season_type": "Playoffs",
                    "game_id": "002250010",
                    "game_date": "2026-02-10",
                    "teams": "NYK / PHI",
                    "game_matchup": "NYK @ PHI",
                    "home_team_abbr": "PHI",
                    "away_team_abbr": "NYK",
                    "home_team_pts": "112",
                    "away_team_pts": "108",
                    "players_played": "20",
                    "player_id": "7",
                    "player_name": "Tyrese Maxey",
                    "team_abbr": "PHI",
                    "opponent_abbr": "NYK",
                    "home_away": "home",
                    "matchup": "PHI vs. NYK",
                    "wl": "W",
                    "min": "36.0",
                    "pts": "31",
                    "reb": "5",
                    "ast": "7",
                    "stl": "1",
                    "blk": "0",
                    "fg_pct": "0.500",
                    "ft_pct": "0.800",
                    "fg3m": "3",
                    "games_sampled": "12",
                    "avg_pts": "25.8",
                    "avg_reb": "4.4",
                    "avg_ast": "6.7",
                    "avg_stl": "1.1",
                    "avg_blk": "0.2",
                    "avg_min": "34.8",
                    "avg_fg_pct": "0.468",
                    "avg_ft_pct": "0.840",
                    "avg_fg3m": "2.4",
                    "pts_delta": "5.2",
                    "reb_delta": "0.6",
                    "ast_delta": "0.3",
                    "stl_delta": "-0.1",
                    "blk_delta": "-0.2",
                    "min_delta": "1.2",
                    "fg_pct_delta": "0.032",
                    "ft_pct_delta": "-0.040",
                    "fg3m_delta": "0.6",
                    "pts_percentile": "82.0",
                    "pts_p10": "18",
                    "pts_p25": "22",
                    "pts_p50": "26",
                    "pts_p75": "30",
                    "pts_p90": "34",
                    "fg_pct_percentile": "72.0",
                    "fg_pct_p10": "0.390",
                    "fg_pct_p25": "0.430",
                    "fg_pct_p50": "0.470",
                    "fg_pct_p75": "0.520",
                    "fg_pct_p90": "0.580",
                    "performance_score": "2.4",
                    "performance_status": "above",
                    "above_count": "3",
                    "below_count": "2",
                    "trend_points_json": json.dumps(
                        [
                            {
                                "game_id": "002250008",
                                "game_date": "2026-02-08",
                                "matchup": "PHI @ BOS",
                                "min": "35.0",
                                "pts": "26",
                                "reb": "4",
                                "ast": "6",
                                "stl": "1",
                                "blk": "0",
                                "fg_pct": "0.480",
                                "ft_pct": "0.750",
                                "fg3m": "2",
                            },
                            {
                                "game_id": "002250010",
                                "game_date": "2026-02-10",
                                "matchup": "PHI vs. NYK",
                                "min": "36.0",
                                "pts": "31",
                                "reb": "5",
                                "ast": "7",
                                "stl": "1",
                                "blk": "0",
                                "fg_pct": "0.500",
                                "ft_pct": "0.800",
                                "fg3m": "3",
                            },
                        ]
                    ),
                }
            ]

    fake_client = FakeClient()
    repo.client = fake_client
    monkeypatch.setattr(
        repo,
        "_query",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(BadRequest("SQL unavailable")),
    )

    initial = repo.get_recent_performance_initial(game_date="2026-02-10")
    detail = repo.get_recent_performance_player(7, game_id="002250010")

    assert initial["players"][0]["player_name"] == "Tyrese Maxey"
    assert detail is not None
    assert detail["metrics"][0]["percentile"] == 82.0
    assert detail["metrics"][0]["range"]["p25"] == 22.0
    detail_metrics = {item["key"]: item for item in detail["metrics"]}
    assert detail_metrics["fg_pct"]["value"] == 50.0
    assert detail_metrics["fg_pct"]["range"]["p25"] == 43.0
    assert detail["trend_30d"]["points"][-1]["pts"] == 31.0
    assert detail["trend_30d"]["points"][-1]["fg_pct"] == 50.0
    assert fake_client.calls == 1


def test_recent_performance_truncated_table_rows_fall_back_to_query(
    monkeypatch,
) -> None:
    repo = _build_repository()

    class TruncatedRowIterator(list):
        total_rows = 9000

    class FakeClient:
        def list_rows(self, table, selected_fields, max_results):
            return TruncatedRowIterator()

    repo.client = FakeClient()
    queries: list[str] = []

    def fake_query(sql, params, *_args, **_kwargs):
        queries.append(sql)
        return []

    monkeypatch.setattr(repo, "_query", fake_query)

    payload = repo.get_recent_performance_initial(game_date="2026-02-10")

    # A page smaller than the table must not feed the row cache (list_rows has
    # no ordering guarantee); the repo should fall back to the query path.
    assert repo._recent_performance_rows_cache is None
    assert queries
    assert payload["players"] == []


def test_get_recent_performance_initial_falls_back_to_live_query(
    monkeypatch,
) -> None:
    repo = _build_repository()
    calls: list[str] = []

    def fake_query(sql, params, *_args, **_kwargs):
        calls.append(sql)
        if "recent_performance_workbench" in sql:
            raise BadRequest("table not found")
        assert "selected_player_ids AS" in sql
        assert "s.game_id = @game_id" in sql
        assert "s.season_type = 'Playoffs'" in sql
        assert "COALESCE(SAFE_CAST(s.min AS FLOAT64), 0) >= 1" in sql
        assert "TO_JSON_STRING(STRUCT(" in sql
        param_names = {param.name for param in params}
        assert {"season", "game_date", "game_id", "limit"} == param_names
        return [
            {
                "section": "date",
                "sort_index": "1",
                "payload": json.dumps({"game_date": "2026-02-10"}),
            },
            {
                "section": "player",
                "sort_index": "1",
                "payload": json.dumps(
                    {
                        "game_id": "002250010",
                        "game_date": "2026-02-10",
                        "player_id": "7",
                        "player_name": "Tyrese Maxey",
                        "team_abbr": "PHI",
                        "opponent_abbr": "NYK",
                        "home_away": "home",
                        "matchup": "PHI vs. NYK",
                        "wl": "W",
                        "min": "36.0",
                        "pts": "31",
                        "reb": "5",
                        "ast": "7",
                        "stl": "1",
                        "blk": "0",
                        "games_sampled": "12",
                        "avg_pts": "25.8",
                        "avg_reb": "4.4",
                        "avg_ast": "6.7",
                        "avg_stl": "1.1",
                        "avg_blk": "0.2",
                        "pts_delta": "5.2",
                        "reb_delta": "0.6",
                        "ast_delta": "0.3",
                        "stl_delta": "-0.1",
                        "blk_delta": "-0.2",
                        "performance_score": "2.4",
                        "performance_status": "above",
                        "above_count": "3",
                        "below_count": "2",
                    }
                ),
            },
        ]

    monkeypatch.setattr(repo, "_query", fake_query)

    payload = repo.get_recent_performance_initial(
        game_date="2026-02-10",
        game_id="002250010",
    )

    assert payload["players"][0]["player_name"] == "Tyrese Maxey"
    assert len(calls) == 2


def test_get_recent_performance_player_prefers_serving_table_detail(
    monkeypatch,
) -> None:
    repo = _build_repository()
    calls: list[str] = []

    def fake_query(sql, params, *_args, **_kwargs):
        calls.append(sql)
        assert "recent_performance_workbench" in sql
        assert "LIMIT 1" in sql
        param_names = {param.name for param in params}
        assert {"season", "player_id", "game_id"} == param_names
        return [
            {
                "game_id": "002250010",
                "game_date": "2026-02-10T00:00:00+00:00",
                "player_id": "7",
                "player_name": "Tyrese Maxey",
                "team_abbr": "PHI",
                "opponent_abbr": "NYK",
                "home_away": "home",
                "matchup": "PHI vs. NYK",
                "wl": "W",
                "min": "36.0",
                "pts": "31",
                "reb": "5",
                "ast": "7",
                "stl": "1",
                "blk": "0",
                "fg_pct": "0.500",
                "ft_pct": "0.800",
                "fg3m": "3",
                "games_sampled": "12",
                "avg_pts": "25.8",
                "avg_reb": "4.4",
                "avg_ast": "6.7",
                "avg_stl": "1.1",
                "avg_blk": "0.2",
                "avg_min": "34.8",
                "avg_fg_pct": "0.468",
                "avg_ft_pct": "0.840",
                "avg_fg3m": "2.4",
                "pts_delta": "5.2",
                "reb_delta": "0.6",
                "ast_delta": "0.3",
                "stl_delta": "-0.1",
                "blk_delta": "-0.2",
                "min_delta": "1.2",
                "fg_pct_delta": "0.032",
                "ft_pct_delta": "-0.040",
                "fg3m_delta": "0.6",
                "pts_percentile": "82.0",
                "pts_p10": "18",
                "pts_p25": "22",
                "pts_p50": "26",
                "pts_p75": "30",
                "pts_p90": "34",
                "fg_pct_percentile": "72.0",
                "fg_pct_p10": "0.390",
                "fg_pct_p25": "0.430",
                "fg_pct_p50": "0.470",
                "fg_pct_p75": "0.520",
                "fg_pct_p90": "0.580",
                "performance_score": "2.4",
                "performance_status": "above",
                "above_count": "3",
                "below_count": "2",
                "trend_points_json": json.dumps(
                    [
                        {
                            "game_id": "002250008",
                            "game_date": "2026-02-08",
                            "matchup": "PHI @ BOS",
                            "min": "35.0",
                            "pts": "26",
                            "reb": "4",
                            "ast": "6",
                            "stl": "1",
                            "blk": "0",
                            "fg_pct": "0.480",
                            "ft_pct": "0.750",
                            "fg3m": "2",
                        },
                        {
                            "game_id": "002250010",
                            "game_date": "2026-02-10",
                            "matchup": "PHI vs. NYK",
                            "min": "36.0",
                            "pts": "31",
                            "reb": "5",
                            "ast": "7",
                            "stl": "1",
                            "blk": "0",
                            "fg_pct": "0.500",
                            "ft_pct": "0.800",
                            "fg3m": "3",
                        },
                    ]
                ),
            }
        ]

    monkeypatch.setattr(repo, "_query", fake_query)

    payload = repo.get_recent_performance_player(7, game_id="002250010")

    assert payload is not None
    assert payload["metrics"][0]["percentile"] == 82.0
    assert payload["metrics"][0]["range"]["p25"] == 22.0
    metrics = {item["key"]: item for item in payload["metrics"]}
    assert metrics["fg_pct"]["value"] == 50.0
    assert metrics["fg_pct"]["range"]["p25"] == 43.0
    assert payload["trend_30d"]["points"][-1]["pts"] == 31.0
    assert payload["trend_30d"]["points"][-1]["fg_pct"] == 50.0
    assert payload["trend_30d"]["stats"][0]["season_average"] == 25.8
    assert len(calls) == 1


def test_get_recent_performance_player_falls_back_from_incomplete_table_detail(
    monkeypatch,
) -> None:
    repo = _build_repository()
    calls: list[str] = []

    complete_row = {
        "game_id": "002250010",
        "game_date": "2026-02-10T00:00:00+00:00",
        "player_id": "7",
        "player_name": "Tyrese Maxey",
        "team_abbr": "PHI",
        "opponent_abbr": "NYK",
        "home_away": "home",
        "matchup": "PHI vs. NYK",
        "wl": "W",
        "min": "36.0",
        "pts": "31",
        "reb": "5",
        "ast": "7",
        "stl": "1",
        "blk": "0",
        "fg_pct": "0.500",
        "ft_pct": "0.800",
        "fg3m": "3",
        "games_sampled": "12",
        "avg_pts": "25.8",
        "avg_reb": "4.4",
        "avg_ast": "6.7",
        "avg_stl": "1.1",
        "avg_blk": "0.2",
        "avg_min": "34.8",
        "avg_fg_pct": "0.468",
        "avg_ft_pct": "0.840",
        "avg_fg3m": "2.4",
        "pts_delta": "5.2",
        "reb_delta": "0.6",
        "ast_delta": "0.3",
        "stl_delta": "-0.1",
        "blk_delta": "-0.2",
        "min_delta": "1.2",
        "fg_pct_delta": "0.032",
        "ft_pct_delta": "-0.040",
        "fg3m_delta": "0.6",
        "performance_score": "2.4",
        "performance_status": "above",
        "above_count": "3",
        "below_count": "2",
    }

    def fake_query(sql, params, *_args, **_kwargs):
        calls.append(sql)
        if "recent_performance_workbench" in sql and "TO_JSON_STRING(STRUCT(" in sql:
            return [
                {
                    "section": "date",
                    "sort_index": "1",
                    "payload": json.dumps({"game_date": "2026-02-10"}),
                },
                {
                    "section": "player",
                    "sort_index": "1",
                    "payload": json.dumps(complete_row),
                },
            ]
        if "recent_performance_workbench" in sql:
            return [
                {
                    "game_id": "002250010",
                    "game_date": "2026-02-10T00:00:00+00:00",
                    "player_id": "7",
                    "player_name": "Tyrese Maxey",
                    "min": "36.0",
                    "pts": "31",
                    "reb": "5",
                    "ast": "7",
                    "stl": "1",
                    "blk": "0",
                    "avg_pts": "25.8",
                    "pts_delta": "5.2",
                    "trend_points_json": "[]",
                }
            ]
        if "trend_window AS" in sql:
            return [complete_row]
        return [complete_row | {"pts_percentile": "82.0", "pts_p25": "22"}]

    monkeypatch.setattr(repo, "_query", fake_query)

    payload = repo.get_recent_performance_player(7, game_id="002250010")

    assert payload is not None
    metrics = {item["key"]: item for item in payload["metrics"]}
    assert metrics["min"]["season_average"] == 34.8
    assert metrics["fg_pct"]["value"] == 50.0
    assert metrics["ft_pct"]["delta"] == -4.0
    assert metrics["fg3m"]["value"] == 3.0
    assert payload["trend_30d"]["points"][-1]["fg_pct"] == 50.0
    assert len(calls) == 3


def test_get_recent_performance_games_uses_dim_game_labels(monkeypatch) -> None:
    repo = _build_repository()

    def fake_query(sql, params, *_args, **_kwargs):
        assert "dim_game" in sql
        assert "stats.game_date = @game_date" in sql
        assert "stats.season_type = 'Playoffs'" in sql
        assert "COALESCE(SAFE_CAST(stats.min AS FLOAT64), 0) >= 1" in sql
        param_names = {param.name for param in params}
        assert param_names == {"season", "game_date"}
        return [
            {
                "game_id": "002250010",
                "game_date": "2026-02-10",
                "teams": "NYK / PHI",
                "matchup": "NYK @ PHI",
                "home_team_abbr": "PHI",
                "away_team_abbr": "NYK",
                "home_team_pts": "112",
                "away_team_pts": "108",
                "players_played": "20",
            }
        ]

    monkeypatch.setattr(repo, "_query", fake_query)

    rows = repo.get_recent_performance_games(game_date="2026-02-10")

    assert rows[0]["matchup"] == "NYK @ PHI"
    assert rows[0]["home_team_pts"] == 112
    assert rows[0]["players_played"] == 20


def test_get_recent_performance_players_builds_baseline_payload(
    monkeypatch,
) -> None:
    repo = _build_repository()

    def fake_query(sql, params, *_args, **_kwargs):
        assert "STDDEV_POP(pts) AS sd_pts" in sql
        assert "performance_score" in sql
        assert "s.game_id = @game_id" in sql
        assert "s.season_type = 'Playoffs'" in sql
        assert "COALESCE(SAFE_CAST(s.min AS FLOAT64), 0) >= 1" in sql
        assert "AVG(fg_pct) AS avg_fg_pct" in sql
        param_names = {param.name for param in params}
        assert {"season", "game_date", "game_id", "limit"} == param_names
        return [
            {
                "game_id": "002250010",
                "game_date": "2026-02-10",
                "player_id": "7",
                "player_name": "Tyrese Maxey",
                "team_abbr": "PHI",
                "opponent_abbr": "NYK",
                "home_away": "home",
                "matchup": "PHI vs. NYK",
                "wl": "W",
                "min": "36.0",
                "pts": "31",
                "reb": "5",
                "ast": "7",
                "stl": "1",
                "blk": "0",
                "fg_pct": "0.500",
                "ft_pct": "0.800",
                "fg3m": "3",
                "games_sampled": "12",
                "avg_pts": "25.8",
                "avg_reb": "4.4",
                "avg_ast": "6.7",
                "avg_stl": "1.1",
                "avg_blk": "0.2",
                "avg_min": "34.8",
                "avg_fg_pct": "0.468",
                "avg_ft_pct": "0.840",
                "avg_fg3m": "2.4",
                "pts_delta": "5.2",
                "reb_delta": "0.6",
                "ast_delta": "0.3",
                "stl_delta": "-0.1",
                "blk_delta": "-0.2",
                "min_delta": "1.2",
                "fg_pct_delta": "0.032",
                "ft_pct_delta": "-0.040",
                "fg3m_delta": "0.6",
                "performance_score": "2.4",
                "performance_status": "above",
                "above_count": "3",
                "below_count": "2",
            }
        ]

    monkeypatch.setattr(repo, "_query", fake_query)

    rows = repo.get_recent_performance_players(
        game_date="2026-02-10", game_id="002250010"
    )

    assert rows[0]["player_id"] == 7
    assert rows[0]["performance_status"] == "above"
    assert rows[0]["metrics"][0]["label"] == "PTS"
    assert rows[0]["metrics"][0]["delta"] == 5.2
    metrics = {item["key"]: item for item in rows[0]["metrics"]}
    assert metrics["fg_pct"]["value"] == 50.0
    assert metrics["ft_pct"]["delta"] == -4.0
    assert metrics["fg3m"]["delta"] == 0.6


def test_get_recent_performance_player_returns_percentile_ranges(
    monkeypatch,
) -> None:
    repo = _build_repository()
    calls: list[str] = []

    def fake_query(sql, params, *_args, **_kwargs):
        calls.append(sql)
        if "recent_performance_workbench" in sql:
            raise BadRequest("table not found")
        if "trend_window AS" in sql:
            assert "WITH selected AS" in sql
            assert "game_id = @game_id" in sql
            assert "DATE_SUB(selected.selected_game_date, INTERVAL 29 DAY)" in sql
            assert "SAFE_CAST(game_date AS DATE)" in sql
            assert "COALESCE(SAFE_CAST(stats.min AS FLOAT64), 0) >= 1" in sql
            assert "stats.fg_pct" in sql
            param_names = {param.name for param in params}
            assert {"season", "player_id", "game_id"} == param_names
            return [
                {
                    "game_id": "002250008",
                    "game_date": "2026-02-08",
                    "matchup": "PHI @ BOS",
                    "min": "35.0",
                    "pts": "26",
                    "reb": "4",
                    "ast": "6",
                    "stl": "1",
                    "blk": "0",
                    "fg_pct": "0.480",
                    "ft_pct": "0.750",
                    "fg3m": "2",
                    "avg_pts": "25.8",
                    "avg_reb": "4.4",
                    "avg_ast": "6.7",
                    "avg_stl": "1.1",
                    "avg_blk": "0.2",
                    "avg_min": "34.8",
                    "avg_fg_pct": "0.468",
                    "avg_ft_pct": "0.840",
                    "avg_fg3m": "2.4",
                },
                {
                    "game_id": "002250010",
                    "game_date": "2026-02-10",
                    "matchup": "PHI vs. NYK",
                    "min": "36.0",
                    "pts": "31",
                    "reb": "5",
                    "ast": "7",
                    "stl": "1",
                    "blk": "0",
                    "fg_pct": "0.500",
                    "ft_pct": "0.800",
                    "fg3m": "3",
                    "avg_pts": "25.8",
                    "avg_reb": "4.4",
                    "avg_ast": "6.7",
                    "avg_stl": "1.1",
                    "avg_blk": "0.2",
                    "avg_min": "34.8",
                    "avg_fg_pct": "0.468",
                    "avg_ft_pct": "0.840",
                    "avg_fg3m": "2.4",
                },
            ]
        assert "APPROX_QUANTILES(r.pts, 100)[OFFSET(25)] AS pts_p25" in sql
        assert "APPROX_QUANTILES(r.fg_pct, 100)[OFFSET(25)] AS fg_pct_p25" in sql
        assert "COUNTIF(r.pts < selected.pts)" in sql
        assert "0.5 * COUNTIF(r.pts = selected.pts)" in sql
        assert "s.season_type = 'Playoffs'" in sql
        assert "COALESCE(SAFE_CAST(stats.min AS FLOAT64), 0) >= 1" in sql
        param_names = {param.name for param in params}
        assert {"season", "player_id", "game_id"} == param_names
        return [
            {
                "game_id": "002250010",
                "game_date": "2026-02-10T00:00:00+00:00",
                "player_id": "7",
                "player_name": "Tyrese Maxey",
                "team_abbr": "PHI",
                "opponent_abbr": "NYK",
                "home_away": "home",
                "matchup": "PHI vs. NYK",
                "wl": "W",
                "min": "36.0",
                "pts": "31",
                "reb": "5",
                "ast": "7",
                "stl": "1",
                "blk": "0",
                "fg_pct": "0.500",
                "ft_pct": "0.800",
                "fg3m": "3",
                "games_sampled": "12",
                "avg_pts": "25.8",
                "avg_reb": "4.4",
                "avg_ast": "6.7",
                "avg_stl": "1.1",
                "avg_blk": "0.2",
                "avg_min": "34.8",
                "avg_fg_pct": "0.468",
                "avg_ft_pct": "0.840",
                "avg_fg3m": "2.4",
                "pts_delta": "5.2",
                "reb_delta": "0.6",
                "ast_delta": "0.3",
                "stl_delta": "-0.1",
                "blk_delta": "-0.2",
                "min_delta": "1.2",
                "fg_pct_delta": "0.032",
                "ft_pct_delta": "-0.040",
                "fg3m_delta": "0.6",
                "pts_percentile": "82.0",
                "pts_p10": "18",
                "pts_p25": "22",
                "pts_p50": "26",
                "pts_p75": "30",
                "pts_p90": "34",
                "fg_pct_percentile": "72.0",
                "fg_pct_p10": "0.390",
                "fg_pct_p25": "0.430",
                "fg_pct_p50": "0.470",
                "fg_pct_p75": "0.520",
                "fg_pct_p90": "0.580",
                "performance_score": "2.4",
                "performance_status": "above",
                "above_count": "3",
                "below_count": "2",
            }
        ]

    monkeypatch.setattr(repo, "_query", fake_query)

    payload = repo.get_recent_performance_player(7, game_id="002250010")

    assert payload is not None
    assert payload["game_date"] == "2026-02-10"
    assert payload["metrics"][0]["percentile"] == 82.0
    assert payload["metrics"][0]["range"]["p25"] == 22.0
    metrics = {item["key"]: item for item in payload["metrics"]}
    assert metrics["fg_pct"]["value"] == 50.0
    assert metrics["fg_pct"]["range"]["p25"] == 43.0
    assert payload["trend_30d"]["window_days"] == 30
    assert payload["trend_30d"]["stats"][0]["season_average"] == 25.8
    assert payload["trend_30d"]["points"][-1]["pts"] == 31.0
    assert payload["trend_30d"]["points"][-1]["fg_pct"] == 50.0
    assert len(calls) == 3


def test_get_recent_performance_player_does_not_synthesize_empty_trend(
    monkeypatch,
) -> None:
    repo = _build_repository()

    def fake_query(sql, params, *_args, **_kwargs):
        if "recent_performance_workbench" in sql:
            raise BadRequest("table not found")
        if "trend_window AS" in sql:
            return []
        return [
            {
                "game_id": "002250010",
                "game_date": "2026-02-10T00:00:00+00:00",
                "player_id": "7",
                "player_name": "Tyrese Maxey",
                "team_abbr": "PHI",
                "opponent_abbr": "NYK",
                "home_away": "home",
                "matchup": "PHI vs. NYK",
                "wl": "W",
                "min": "36.0",
                "pts": "31",
                "reb": "5",
                "ast": "7",
                "stl": "1",
                "blk": "0",
                "games_sampled": "12",
                "avg_pts": "25.8",
                "avg_reb": "4.4",
                "avg_ast": "6.7",
                "avg_stl": "1.1",
                "avg_blk": "0.2",
                "pts_delta": "5.2",
                "reb_delta": "0.6",
                "ast_delta": "0.3",
                "stl_delta": "-0.1",
                "blk_delta": "-0.2",
                "performance_score": "2.4",
                "performance_status": "above",
                "above_count": "3",
                "below_count": "2",
            }
        ]

    monkeypatch.setattr(repo, "_query", fake_query)

    payload = repo.get_recent_performance_player(7, game_id="002250010")

    assert payload is not None
    assert payload["trend_30d"]["points"] == []
    assert payload["trend_30d"]["stats"][0]["season_average"] is None


def test_get_metric_leaders_uses_allowlisted_metric_sql(monkeypatch) -> None:
    repo = _build_repository()

    def fake_query(sql, params, *_args, **_kwargs):
        assert "player_category_profile" in sql
        assert "ROUND(avg_pts, 2) AS metric_value" in sql
        assert "ORDER BY avg_pts DESC" in sql
        param_names = {param.name for param in params}
        assert {"season", "metric_key", "metric_label", "limit"} <= param_names
        return [
            {
                "season": "2025-26",
                "player_id": "7",
                "player_name": "Tyrese Maxey",
                "team_abbr": "PHI",
                "games_sampled": "12",
                "sample_status": "ready",
                "metric_key": "pts",
                "metric_label": "PTS",
                "metric_value": "28.4",
                "percentile": "91.0",
            }
        ]

    monkeypatch.setattr(repo, "_query", fake_query)

    rows = repo.get_metric_leaders("pts", limit=10)

    assert rows[0]["player_id"] == 7
    assert rows[0]["metric_value"] == 28.4
    assert rows[0]["percentile"] == 91.0


def test_get_metric_leaders_rejects_unknown_metric() -> None:
    repo = _build_repository()

    with pytest.raises(ValueError):
        repo.get_metric_leaders("pts; drop table")


def test_get_metric_leaders_falls_back_to_legacy_category_profile_schema(
    monkeypatch,
) -> None:
    repo = _build_repository()
    calls: list[str] = []

    def fake_query(sql, *_args, **_kwargs):
        calls.append(sql)
        if "AND is_qualified" in sql:
            raise BadRequest("Unrecognized name: is_qualified")
        assert "games_sampled >= 5" in sql
        assert "NULL AS percentile" in sql
        return [
            {
                "season": "2025-26",
                "player_id": "7",
                "player_name": "Tyrese Maxey",
                "team_abbr": "PHI",
                "games_sampled": "12",
                "sample_status": "ready",
                "metric_key": "ast",
                "metric_label": "AST",
                "metric_value": "7.4",
                "percentile": None,
            }
        ]

    monkeypatch.setattr(repo, "_query", fake_query)

    rows = repo.get_metric_leaders("ast", limit=10)

    assert len(calls) == 2
    assert rows[0]["metric_label"] == "AST"
    assert rows[0]["metric_value"] == 7.4
    assert rows[0]["percentile"] is None


def test_get_player_metric_percentile_returns_requested_cohort_result(
    monkeypatch,
) -> None:
    repo = _build_repository()

    def fake_query(sql, params, *_args, **_kwargs):
        assert "PERCENT_RANK()" in sql
        assert "games_sampled >= @min_games" in sql
        assert "avg_pts" in sql
        assert "avg_ast" in sql
        assert "points_created" not in sql.replace("@metric_key", "")
        param_names = {param.name for param in params}
        assert {
            "season",
            "player_id",
            "metric_key",
            "metric_label",
            "min_games",
        } <= param_names
        return [
            {
                "season": "2025-26",
                "player_id": "2544",
                "player_name": "LeBron James",
                "team_abbr": "LAL",
                "games_sampled": "70",
                "metric_key": "points_created",
                "metric_label": "Attributed Points",
                "metric_value": "46.2",
                "min_games": "50",
                "cohort_rank": "4",
                "percentile": "98.1",
                "cohort_size": "100",
                "cohort_avg": "24.4",
                "player_count": "502",
                "max_games_sampled": "82",
            }
        ]

    monkeypatch.setattr(repo, "_query", fake_query)

    payload = repo.get_player_metric_percentile(2544, "points_created", min_games=50)

    assert payload is not None
    assert payload["metric_value"] == 46.2
    assert payload["percentile"] == 98.1
    assert payload["in_requested_cohort"] is True


def test_get_player_metric_percentile_rejects_unknown_metric() -> None:
    repo = _build_repository()

    with pytest.raises(ValueError):
        repo.get_player_metric_percentile(2544, "unsafe_metric")


def test_get_similarity_map_decorates_rows_and_summarizes(monkeypatch) -> None:
    repo = _build_repository()

    axes_json = json.dumps(
        [
            {"key": "proj_x", "variance": 0.28, "drivers": ["scoring volume", "usage"]},
            {"key": "proj_y", "variance": 0.19, "drivers": ["rim protection"]},
            {"key": "proj_z", "variance": 0.11, "drivers": ["playmaking"]},
        ]
    )

    def fake_query(sql, *_args, **_kwargs):
        # Axis metadata is fetched in a separate, guarded query.
        if "projection_axes" in sql and "proj_x" not in sql:
            return [{"projection_axes": axes_json}]
        assert "proj_x" in sql
        assert "sample_status IN ('ready', 'limited_sample')" in sql
        return [
            {
                "player_id": 1,
                "player_name": "Alpha",
                "team_abbr": "AAA",
                "archetype_id": "cluster_0",
                "archetype_label": "Scoring Guard - Scoring Volume / Recent Scoring",
                "cluster_confidence": 0.8,
                "top_traits": "scoring, shooting",
                "games_sampled": 20,
                "sample_status": "ready",
                "proj_x": 0.1,
                "proj_y": 0.2,
                "proj_z": 0.3,
            },
            {
                "player_id": 2,
                "player_name": "Bravo",
                "team_abbr": "BBB",
                "archetype_id": "cluster_1",
                "archetype_label": "Scoring Guard - Shot Volume / Three-Point Diet",
                "cluster_confidence": 0.5,
                "top_traits": "",
                "games_sampled": 10,
                "sample_status": "limited_sample",
                "proj_x": -0.4,
                "proj_y": 0.0,
                "proj_z": 0.1,
            },
        ]

    monkeypatch.setattr(repo, "_query", fake_query)
    result = repo.get_similarity_map()

    assert len(result["players"]) == 2
    first = result["players"][0]
    assert first["player_id"] == 1
    assert (first["x"], first["y"], first["z"]) == (0.1, 0.2, 0.3)
    assert first["top_traits"] == ["scoring", "shooting"]
    # Per-player rows keep the granular label...
    assert first["archetype_label"] == "Scoring Guard - Scoring Volume / Recent Scoring"
    # ...but the summary collapses both to the base archetype family.
    assert result["archetypes"] == [{"archetype_label": "Scoring Guard", "count": 2}]
    # Axis metadata is parsed from the projection_axes JSON.
    assert [axis["key"] for axis in result["axes"]] == ["proj_x", "proj_y", "proj_z"]
    assert result["axes"][0]["variance"] == 0.28
    assert result["axes"][0]["drivers"] == ["scoring volume", "usage"]


def test_player_detail_guards_stale_big_archetype_for_point_guard() -> None:
    repo = _build_repository()
    stale_label = "Stretch Big - Rebounding / Rim Protection"
    archetype_row = {
        "sample_status": "ready",
        "archetype_id": "cluster_big",
        "archetype_label": stale_label,
        "archetype_summary": f"{stale_label} driven by rebounding, rim protection.",
        "position": "PG",
        "height_inches": 78,
        "weight_lbs": 215,
        "wingspan_inches": 82,
        "norm_season_avg_ast": 0.62,
        "norm_recent_ast": 0.6,
        "norm_team_ast_contribution_rate": 0.52,
        "norm_team_offense_contribution_rate": 0.24,
        "model_results_json": json.dumps(
            {
                "models": [
                    {
                        "model_key": "gmm",
                        "model_label": "Gaussian mixture",
                        "archetype_label": stale_label,
                        "archetype_summary": (
                            f"{stale_label} driven by rebounding, rim protection."
                        ),
                        "is_recommended": True,
                    }
                ]
            }
        ),
    }

    payload = repo._build_player_detail_payload(
        identity={
            "player_id": 1642277,
            "player_name": "Dylan Harper",
            "latest_season": "2025-26",
            "latest_team_abbr": "SAS",
            "games_sampled": 12,
            "sample_status": "ready",
            "is_qualified": True,
        },
        row=None,
        archetype_row=archetype_row,
        similarity_state="fresh",
        similarity_reason=None,
        similar_players=[],
        game_log={"games": []},
        trends=[],
        chart_baselines={},
    )

    assert payload["archetype"]["archetype_label"].startswith("Secondary Creator")
    assert "Stretch Big" not in payload["archetype"]["summary"]
    assert payload["similarity_models"][0]["archetype_label"].startswith(
        "Secondary Creator"
    )


def test_get_similar_players_guards_stale_big_candidate_label(monkeypatch) -> None:
    repo = _build_repository()
    anchor = {
        "player_id": 1,
        "player_name": "Anchor",
        "sample_status": "ready",
    }

    def fake_query(*_args, **_kwargs):
        return [
            {
                "player_id": 1642277,
                "player_name": "Dylan Harper",
                "team_abbr": "SAS",
                "position": "PG",
                "height_inches": 78,
                "weight_lbs": 215,
                "wingspan_inches": 82,
                "archetype_label": "Stretch Big - Rebounding",
                "sample_status": "ready",
                "similarity_score": 0.89,
                "norm_season_avg_ast": 0.62,
                "norm_recent_ast": 0.6,
                "norm_team_ast_contribution_rate": 0.52,
                "norm_team_offense_contribution_rate": 0.24,
            }
        ]

    monkeypatch.setattr(repo, "_query", fake_query)

    state, reason, items = repo._get_similar_players(1, anchor=anchor)

    assert state == "fresh"
    assert reason is None
    assert items[0]["archetype_label"].startswith("Secondary Creator")


def test_get_similarity_map_loads_players_when_axes_column_missing(monkeypatch) -> None:
    repo = _build_repository()

    def fake_query(sql, *_args, **_kwargs):
        # Simulate a table published before the projection_axes column existed.
        if "projection_axes" in sql:
            raise BadRequest("Unrecognized name: projection_axes")
        return [
            {
                "player_id": 1,
                "player_name": "Alpha",
                "team_abbr": "AAA",
                "archetype_id": "cluster_0",
                "archetype_label": "Scoring Guard",
                "cluster_confidence": 0.8,
                "top_traits": "scoring",
                "games_sampled": 20,
                "sample_status": "ready",
                "proj_x": 0.1,
                "proj_y": 0.2,
                "proj_z": 0.3,
            }
        ]

    monkeypatch.setattr(repo, "_query", fake_query)
    result = repo.get_similarity_map()

    # Players still load; only the axis annotations degrade to empty.
    assert len(result["players"]) == 1
    assert result["axes"] == []


def test_get_similarity_map_returns_empty_on_bigquery_error(monkeypatch) -> None:
    repo = _build_repository()

    def fake_query(*_args, **_kwargs):
        raise BadRequest("boom")

    monkeypatch.setattr(repo, "_query", fake_query)
    result = repo.get_similarity_map()

    assert result["players"] == []
    assert result["archetypes"] == []


def test_get_similarity_neighbors_returns_anchor_and_neighbors(monkeypatch) -> None:
    repo = _build_repository()
    monkeypatch.setattr(
        repo, "_fetch_similarity_anchor", lambda pid: {"player_name": "Anchor"}
    )

    def fake_similar(player_id, *, anchor=None, limit=6):
        assert anchor == {"player_name": "Anchor"}
        assert limit == 4
        return (
            "fresh",
            None,
            [
                {
                    "player_id": 2,
                    "player_name": "Match",
                    "team_abbr": "BBB",
                    "archetype_label": "Scoring Guard",
                    "similarity_score": 0.9,
                    "shared_traits": ["scoring"],
                }
            ],
        )

    monkeypatch.setattr(repo, "_get_similar_players", fake_similar)
    result = repo.get_similarity_neighbors(7, limit=4)

    assert result["player_id"] == 7
    assert result["player_name"] == "Anchor"
    assert result["state"] == "fresh"
    assert result["neighbors"][0]["player_id"] == 2
    assert result["neighbors"][0]["similarity_score"] == 0.9


def test_get_similarity_neighbors_unavailable_when_anchor_missing(monkeypatch) -> None:
    repo = _build_repository()
    monkeypatch.setattr(repo, "_fetch_similarity_anchor", lambda pid: None)

    result = repo.get_similarity_neighbors(7)

    assert result["state"] == "unavailable"
    assert result["neighbors"] == []
