from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd
import pytest
from app.config import Settings, get_settings
from app.freshness import build_publication_health
from app.main import app
from app.seasons import current_season, season_bounds, settings_for_season
from fastapi.testclient import TestClient
from scripts.backfill_historical_seasons import reconcile_games, warehouse_env


def settings():
    return Settings(
        project_id="example",
        gold_dataset="gold",
        metadata_dataset="meta",
        freshness_threshold_hours=36,
        max_search_results=12,
    )


def test_archive_settings_do_not_mutate_current_settings():
    base = settings()
    archived = settings_for_season(base, "2023-24")
    assert archived.gold_dataset == "gold_2023_24"
    assert archived.agent_dataset == "nba_agent_2023_24"
    assert base.gold_dataset == "gold"
    assert settings_for_season(base, "2025-26") == base
    with pytest.raises(ValueError):
        settings_for_season(base, "2023-24`")
    assert tuple(str(value) for value in season_bounds("2024-25")) == (
        "2024-07-01",
        "2025-06-30",
    )


def test_writer_refuses_current_season_and_isolates_every_dataset():
    with pytest.raises(ValueError):
        warehouse_env("2025-26", "example", "US")
    env = warehouse_env("2024-25", "example", "US")
    for name in (
        "BQ_DATASET_BRONZE",
        "BQ_DATASET_SILVER",
        "BQ_DATASET_GOLD",
        "BQ_DATASET_AGENT",
        "BQ_METADATA_DATASET",
    ):
        assert env[name].endswith("_2024_25")


def test_regular_season_reconciliation_rejects_missing_or_mismatched_games():
    rows = []
    for game in range(1230):
        for team in (game % 30, (game + 1) % 30):
            rows.append(
                {
                    "GAME_ID": f"00223{game + 1:05d}",
                    "TEAM_ID": team,
                    "MATCHUP": f"{team}-{game}",
                    "PTS": 100,
                    "GAME_DATE": "2024-01-01",
                }
            )
    teams = pd.DataFrame(rows)
    players = teams.assign(
        PLAYER_ID=teams.TEAM_ID, SEASON="2023-24", SEASON_TYPE="Regular Season"
    )
    assert reconcile_games(players, teams, "2023-24", "Regular Season")["games"] == 1230
    with pytest.raises(ValueError, match="coverage mismatch"):
        reconcile_games(players.iloc[2:], teams, "2023-24", "Regular Season")
    wrong = players.copy()
    wrong.loc[0, "PTS"] = 99
    with pytest.raises(ValueError, match="totals/dates"):
        reconcile_games(wrong, teams, "2023-24", "Regular Season")
    with pytest.raises(ValueError, match="season or phase"):
        reconcile_games(
            players.assign(SEASON="2024-25"), teams, "2023-24", "Regular Season"
        )


def test_historical_health_is_an_archive_and_missing_injuries_stay_unavailable():
    health = build_publication_health(
        None,
        None,
        {"game_count": 1312, "latest_game_date": "2024-06-17"},
        settings=settings_for_season(settings(), "2023-24"),
        now=datetime(2026, 11, 1, tzinfo=UTC),
    )
    assert health["season"] == "2023-24"
    assert health["status"] == "historical"
    assert health["updates_expected"] is False
    assert health["assets"]["injuries"]["status"] == "unavailable"


def test_request_seasons_are_isolated_and_validated(monkeypatch):
    import importlib

    main = importlib.import_module("app.main")
    old = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    app.dependency_overrides[get_settings] = settings
    monkeypatch.setattr(
        main,
        "_cached_repository",
        lambda config: SimpleNamespace(
            get_rankings=lambda **kwargs: [
                {"season": config.season, "dataset": config.gold_dataset}
            ]
        ),
    )
    try:
        client = TestClient(app)

        def read(season):
            payload = client.get(f"/api/rankings?season={season}").json()
            assert payload["season"] == season
            assert payload["items"][0]["season"] == season
            return payload

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(read, ["2023-24", "2024-25", "2025-26"] * 3))
        assert results[0]["items"][0]["dataset"] == "gold_2023_24"
        assert client.get("/api/rankings?season=2022-23").status_code == 422
        assert client.get("/api/rankings").json()["season"] == "2025-26"
        assert current_season() == "2025-26"
        page = client.get("/performance?season=2023-24")
        assert page.status_code == 200
        assert "Explore 2023-24 playoff game performances" in page.text
        assert "data-season-selector" not in page.text
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)


def test_agent_player_resolution_cache_does_not_cross_seasons():
    from app.agent.tools import StatsToolRunner

    def repo(season, player_id):
        return SimpleNamespace(
            settings=settings_for_season(settings(), season),
            search_players=lambda query, limit: [
                {"player_id": player_id, "player_name": "Archive Cache Test"}
            ],
        )

    current = StatsToolRunner(repo("2025-26", 1)).resolve_player("Archive Cache Test")
    archived = StatsToolRunner(repo("2023-24", 2)).resolve_player("Archive Cache Test")
    assert current["player"]["player_id"] == 1
    assert archived["player"]["player_id"] == 2


def test_historical_templates_load_season_aware_modules():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for name in ("performance", "similarity_map"):
        html = (root / f"app/templates/{name}.html").read_text()
        assert f'<script type="module" src="/static/{name}.js' in html


def test_legacy_injury_header_uses_actual_time_and_rejects_wrong_day():
    from scripts.historical_sources import report_time_from_header

    assert (
        report_time_from_header("Injury\nReport:\n10/24/23\n05:30\nPM", "2023-10-24")
        == "05_30PM"
    )
    with pytest.raises(ValueError):
        report_time_from_header("Injury Report: 10/24/23 05:30 PM", "2024-10-24")


def test_neutral_game_home_away_comes_from_official_game_context(tmp_path):
    import json

    from scripts.historical_sources import resolve_neutral_schedule

    frame = pd.DataFrame(
        [
            {"GAME_ID": "0022400147", "TEAM_ABBR": "WAS", "HOME_AWAY": "AWAY"},
            {"GAME_ID": "0022400147", "TEAM_ABBR": "MIA", "HOME_AWAY": "AWAY"},
        ]
    )
    (tmp_path / "2024-25_game_context.json").write_text(
        json.dumps(
            [
                {
                    "GAME_ID": "0022400147",
                    "HOME_TEAM_ID": 1610612764,
                    "VISITOR_TEAM_ID": 1610612748,
                }
            ]
        )
    )
    result = resolve_neutral_schedule("2024-25", frame, tmp_path)
    assert result.set_index("TEAM_ABBR").HOME_AWAY.to_dict() == {
        "WAS": "HOME",
        "MIA": "AWAY",
    }
    assert frame.HOME_AWAY.eq("AWAY").all()


def test_historical_injury_names_do_not_guess_explicit_suffixes(monkeypatch):
    from scripts.historical_sources import resolve_injury_names

    import nba_pipeline as pipeline

    monkeypatch.setattr(
        pipeline.players,
        "get_players",
        lambda: [
            {"id": 1, "full_name": "Jimmy Butler III"},
            {"id": 2, "full_name": "LeBron James"},
        ],
    )
    frame = pd.DataFrame(
        {
            "PLAYER_ID": pd.Series([pd.NA, pd.NA], dtype="Int64"),
            "PLAYER_NAME_SOURCE": ["Butler, Jimmy", "James Jr., LeBron"],
            "PLAYER_NAME": ["Jimmy Butler", "LeBron James Jr."],
        }
    )
    logs = pd.DataFrame(columns=["PLAYER_ID", "PLAYER_NAME"])
    result, count = resolve_injury_names(frame, logs)
    assert count == 1
    assert result.loc[0, "PLAYER_ID"] == 1
    assert pd.isna(result.loc[1, "PLAYER_ID"])


def test_performance_reader_uses_physical_column_order():
    from types import SimpleNamespace

    from app.repository import BigQueryWarehouseRepository
    from google.cloud import bigquery
    from google.cloud.bigquery.table import Row

    # A rebuilt table puts quantiles before deltas; the old declaration did
    # the reverse and decoded a percentile as a player's stat difference.
    schema = [
        bigquery.SchemaField(name, "FLOAT")
        for name in ["pts", "pts_p10", "pts_delta", "fg_pct_delta"]
    ]
    values = [9.0, 8.0, -9.3, 0.148]

    class Client:
        def get_table(self, table_id):
            return SimpleNamespace(schema=schema)

        def list_rows(self, table_id, selected_fields, max_results):
            return [Row(values, {f.name: i for i, f in enumerate(selected_fields)})]

    repo = BigQueryWarehouseRepository(settings(), client=Client())
    rows = repo._fetch_recent_performance_table_rows_api()
    assert float(rows[0]["pts_delta"]) == -9.3
    assert float(rows[0]["fg_pct_delta"]) == 0.148


@pytest.mark.parametrize("failure", ["http", "timeout", "header", "parser"])
def test_injury_extract_records_failed_day_and_preserves_success(
    tmp_path, monkeypatch, failure
):
    import json
    from collections import Counter

    import requests
    from scripts import historical_sources as sources

    season = "2023-24"
    for phase in ("Regular_Season", "Playoffs"):
        pd.DataFrame({"GAME_DATE": ["2023-10-24"]}).to_parquet(
            tmp_path / f"{season}_{phase}_players.parquet"
        )
    monkeypatch.setattr(sources.pipeline, "build_player_id_lookup", lambda: {})
    monkeypatch.setattr(sources.time, "sleep", lambda _: None)
    calls = Counter()

    def get(url, **kwargs):
        day = url.split("Injury-Report_")[1][:10]
        calls[day] += 1
        if day == "2023-10-23" and failure == "timeout":
            raise requests.Timeout("source timeout")
        response = requests.Response()
        response.status_code = 503 if day == "2023-10-23" and failure == "http" else 200
        response.url = url
        response._content = day.encode()
        return response

    def extract(content):
        day = content.decode()
        if day == "2023-10-23" and failure == "parser":
            raise RuntimeError("Malformed PDF")
        # The first day's PDF incorrectly claims the following report date.
        return "Injury Report: 10/24/23 05:30 PM"

    monkeypatch.setattr(sources.requests, "get", get)
    monkeypatch.setattr(
        sources.pipeline, "extract_text_from_injury_report_pdf", extract
    )
    monkeypatch.setattr(
        sources.pipeline,
        "parse_injury_report_text",
        lambda text, **kwargs: pd.DataFrame({"REPORT_DATE": [kwargs["report_date"]]}),
    )
    sources.extract_injuries(season, tmp_path)
    output = tmp_path / f"{season}_injuries.parquet"
    assert pd.read_parquet(output).REPORT_DATE.tolist() == ["2023-10-24"]
    checks = json.loads((tmp_path / f"{season}_injury_source_checks.json").read_text())
    assert checks[0]["status"] == "error"
    assert checks[0]["rows"] == 0
    assert checks[0]["error_type"]
    assert checks[0]["attempts"] == (3 if failure in ("http", "timeout") else 1)
    assert checks[1]["status"] == 200
    assert checks[1]["rows"] == 1
    assert calls["2023-10-24"] == 1
    # Retrying revisits the failure and reuses the successful daily cache.
    first_calls = calls["2023-10-23"]
    output.unlink()
    sources.extract_injuries(season, tmp_path)
    assert calls["2023-10-23"] == 2 * first_calls
    assert calls["2023-10-24"] == 1


def test_injury_extract_all_unavailable_writes_empty_data_and_audit(
    tmp_path, monkeypatch
):
    import json

    import requests
    from scripts import historical_sources as sources

    season = "2023-24"
    for phase in ("Regular_Season", "Playoffs"):
        pd.DataFrame({"GAME_DATE": ["2023-10-24"]}).to_parquet(
            tmp_path / f"{season}_{phase}_players.parquet"
        )
    monkeypatch.setattr(sources.pipeline, "build_player_id_lookup", lambda: {})
    monkeypatch.setattr(sources.time, "sleep", lambda _: None)

    def get(url, **kwargs):
        response = requests.Response()
        response.status_code = 404 if "2023-10-23" in url else 503
        response.url = url
        return response

    monkeypatch.setattr(sources.requests, "get", get)
    sources.extract_injuries(season, tmp_path)
    frame = pd.read_parquet(tmp_path / f"{season}_injuries.parquet")
    assert frame.empty
    assert list(frame.columns) == [
        f.name for f in sources.pipeline.get_injury_report_schema()
    ]
    checks = json.loads((tmp_path / f"{season}_injury_source_checks.json").read_text())
    assert [c["status"] for c in checks] == [404, "error"]
    assert all(c["rows"] == 0 for c in checks)
