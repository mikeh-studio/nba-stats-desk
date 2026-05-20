from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from google.api_core.exceptions import BadRequest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.repository import (
    BigQueryWarehouseRepository,
    SIMILARITY_FEATURE_COLUMNS,
    build_analysis_payload,
    build_freshness_payload,
    _weighted_similarity_vector,
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
        assert {"season", "player_id", "metric_key", "metric_label", "min_games"} <= param_names
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
