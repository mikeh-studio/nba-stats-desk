"""Rollover configuration, coverage fallback, and archive publication guards."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from app.agent.semantics import SemanticError
from app.config import Settings
from app.research import available_research
from app.seasons import ARCHIVE_SEASONS, DEFAULT_SEASON, SEASONS, settings_for_season
from dags.season_config import CURRENT_SEASON, SUPPORTED_SEASONS
from scripts.archive_season_copy import (
    archive_mapping,
    copy_archive,
    rebind_view,
    validate_games,
)

ROOT = Path(__file__).resolve().parents[1]


def test_configuration_and_archive_routing_agree():
    config = yaml.safe_load((ROOT / "dbt_project.yml").read_text())["vars"]
    js = (ROOT / "app/static/season.js").read_text()
    assert (
        yaml.safe_load((ROOT / "season.yml").read_text())["current_season"]
        == DEFAULT_SEASON
        == CURRENT_SEASON
        == config["nba_current_season"]
    )
    assert (
        re.search(r'export const DEFAULT_SEASON = "([^"]+)"', js)[1] == DEFAULT_SEASON
    )
    assert tuple(config["nba_supported_seasons"]) == SUPPORTED_SEASONS == SEASONS
    base = Settings(
        project_id="example",
        gold_dataset="nba_gold",
        metadata_dataset="nba_metadata",
        freshness_threshold_hours=36,
        max_search_results=12,
    )
    assert settings_for_season(base, DEFAULT_SEASON) == base
    archive = settings_for_season(base, "2025-26")
    assert archive.gold_dataset == "nba_gold_2025_26"
    assert archive.agent_dataset == "nba_agent_2025_26"
    assert archive.metadata_dataset == "nba_metadata_2025_26"
    assert "_2025-26" in archive.agent_history_path
    for path in (ROOT / "contracts").glob("*.yml"):
        assert not re.search(r"20\d{2}-\d{2}(?:-\d{2})?", path.read_text()), path


def test_missing_coverage_falls_back_but_access_and_invalid_evidence_do_not(
    monkeypatch,
):
    from app import research

    calls = []

    class Evidence:
        def __init__(self, season):
            self.rows = [{"season": season}]

        def validate(self):
            pass

    def load(settings, repo, season):
        calls.append(season)
        if season == DEFAULT_SEASON:
            raise SemanticError("unsupported_coverage", "empty season")
        return Evidence(season)

    monkeypatch.setattr(research, "load_research_evidence", load)
    assert (
        available_research(None, None, DEFAULT_SEASON, fallback=True)[1]
        == ARCHIVE_SEASONS[0]
    )
    assert calls == [DEFAULT_SEASON, ARCHIVE_SEASONS[0]]
    with pytest.raises(SemanticError, match="No matching"):
        available_research(None, None, DEFAULT_SEASON)
    for code in ("access_denied", "invalid_evidence"):
        calls.clear()

        def fail(*args):
            calls.append(args[-1])
            raise SemanticError(code, "failure")

        monkeypatch.setattr(research, "load_research_evidence", fail)
        with pytest.raises(SemanticError, match="failure"):
            available_research(None, None, DEFAULT_SEASON, fallback=True)
        assert calls == [DEFAULT_SEASON]


def test_archive_rebind_and_no_source_overwrites():
    mapping = archive_mapping("example-project", "2025-26")
    assert all(s != d and d.endswith("_2025_26") for s, d in mapping.items())
    sql = rebind_view(
        "SELECT * FROM `example-project.nba_gold.dim_game` JOIN nba_silver.games USING(game_id)",
        mapping,
    )
    assert "example-project.nba_gold_2025_26.dim_game" in sql
    assert "example-project.nba_silver_2025_26.games" in sql
    with pytest.raises(ValueError):
        archive_mapping("example-project", DEFAULT_SEASON)

    class Client:
        def get_dataset(self, name):
            return SimpleNamespace(location="US")

    with pytest.raises(ValueError, match="Destination already exists"):
        copy_archive(Client(), "example-project", "2025-26", publish=True)


def test_archive_rejects_partial_wrong_season_and_duplicate_games():
    for games in (
        [],
        [
            {
                "game_id": "0022500001",
                "season": DEFAULT_SEASON,
                "winning_team_abbr": "ATL",
            }
        ],
        [dict(game_id="0022500001", season="2025-26", winning_team_abbr="ATL")] * 2,
    ):
        with pytest.raises(ValueError):
            validate_games(games, "2025-26")


def test_copy_publication_requires_parity_and_publishes_manifest_last(monkeypatch):
    from google.api_core.exceptions import NotFound
    from scripts import archive_season_copy as archive

    events = []
    games = [
        dict(game_id=f"00225{i:05}", season="2025-26", winning_team_abbr="ATL")
        for i in range(1230)
    ]
    games += [
        dict(game_id=f"00425{s:03}{g}", season="2025-26", winning_team_abbr="ATL")
        for s in range(15)
        for g in range(1, 5)
    ]
    tables = {
        "nba_gold": ["dim_game", "fct_player_game_stats"],
        "nba_bronze": ["raw_player_injury_reports"],
    }

    class Job:
        def result(self, **kwargs):
            pass

    class Client:
        def get_dataset(self, name):
            if name.endswith("_2025_26"):
                raise NotFound("absent")
            return SimpleNamespace(location="US")

        def list_tables(self, name):
            return [
                SimpleNamespace(reference=f"{name}.{t}")
                for t in tables.get(name.split(".")[-1], [])
            ]

        def get_table(self, name):
            return SimpleNamespace(
                table_id=name.split(".")[-1], table_type="TABLE", etag="stable"
            )

        def create_dataset(self, dataset):
            events.append(("dataset", dataset.dataset_id))

        def copy_table(self, source, dest, **kwargs):
            assert "_2025_26." in dest and "@" in source
            assert kwargs["job_config"].write_disposition == "WRITE_EMPTY"
            events.append(("copy", dest))
            return Job()

        def load_table_from_json(self, data, dest, **kwargs):
            events.append(("manifest", dest))
            report = json.loads(data[0]["report_json"])
            assert report["validated_assets"] == ["stats"]
            assert report["row_counts"]["injury_reports"] == 0
            assert kwargs["job_config"].write_disposition == "WRITE_EMPTY"
            return Job()

    monkeypatch.setattr(
        archive,
        "rows",
        lambda client, sql: (
            games if "winning_team_abbr" in sql else [{"d": "2026-06-20"}]
        ),
    )
    monkeypatch.setattr(
        archive,
        "fingerprint",
        lambda client, table: dict(n=0 if "injury" in table else 10, h=42),
    )
    assert (
        copy_archive(Client(), "example-project", "2025-26")["method"]
        == "warehouse_copy"
    )
    assert events == []
    with pytest.raises(ValueError, match="Stop all writers"):
        copy_archive(Client(), "example-project", "2025-26", publish=True)
    assert events == []
    copy_archive(
        Client(), "example-project", "2025-26", publish=True, writers_stopped=True
    )
    assert events[-1][0] == "manifest"
    events.clear()
    monkeypatch.setattr(
        archive,
        "fingerprint",
        lambda client, table: dict(n=10 if "@" in table else 9, h=42),
    )
    with pytest.raises(ValueError, match="parity"):
        copy_archive(
            Client(), "example-project", "2025-26", publish=True, writers_stopped=True
        )
    assert not any(event[0] == "manifest" for event in events)


@pytest.mark.parametrize(
    "domain",
    [
        "game_logs",
        "game_line_scores",
        "schedule",
        "injury_reports",
        "player_shot_locations",
    ],
)
def test_current_contract_frames_and_explicit_historical_bounds(domain):
    import pandas as pd
    from tests.test_source_contracts import (
        _game_log_row,
        _injury_report_row,
        _line_score_row,
        _schedule_rows,
        _shot_location_row,
    )

    import nba_source_contracts as contracts

    inputs = {
        "game_logs": [_game_log_row()],
        "game_line_scores": [
            _line_score_row(),
            _line_score_row(TEAM_ID=1610612738, TEAM_ABBR="BOS"),
        ],
        "schedule": _schedule_rows(),
        "injury_reports": [_injury_report_row()],
        "player_shot_locations": [_shot_location_row()],
    }
    old = pd.DataFrame(inputs[domain])
    assert (
        contracts.validate_source_contract(domain, old, season="2025-26").result[
            "fatal_count"
        ]
        == 0
    )
    with pytest.raises(contracts.SourceContractError):
        contracts.validate_source_contract(domain, old)
    current = old.copy()
    current["SEASON"] = DEFAULT_SEASON
    for column in ("GAME_DATE", "SCHEDULE_DATE"):
        if column in current:
            current[column] = "2026-10-20"
    result = contracts.validate_source_contract(domain, current)
    assert result.result["fatal_count"] == 0
    assert result.quarantine_frame.empty


def test_view_dependencies_are_rebound_sorted_and_external_references_refused():
    from scripts.archive_season_copy import ordered_views

    mapping = archive_mapping("example-project", "2025-26")

    def table(name, sql):
        return SimpleNamespace(table_id=name, table_type="VIEW", view_query=sql)

    src, dest = "example-project.nba_gold", mapping["example-project.nba_gold"]
    inventory = [
        (table("child", f"SELECT * FROM `{src}.parent`"), "US", src, dest),
        (table("parent", "SELECT 1 AS value"), "US", src, dest),
    ]
    assert [name for name, _ in ordered_views(inventory, mapping)] == [
        dest + ".parent",
        dest + ".child",
    ]
    inventory[0][0].view_query = "SELECT * FROM `other-project.nba_gold.live`"
    with pytest.raises(ValueError, match="external"):
        ordered_views(inventory, mapping)


def test_actual_research_endpoints_disclose_fallback_and_preserve_explicit_scope(
    monkeypatch,
):
    from app import main
    from tests.test_api import _test_settings, build_client
    from tests.test_research import evidence

    calls = []

    def load(settings, repo, season):
        calls.append(season)
        # Model the transition: base data still contains last season's rows.
        return evidence()

    monkeypatch.setattr(main, "load_research_evidence", load)
    client = build_client(settings=_test_settings(research_snapshot_path="fixture"))
    try:
        players = client.get("/api/research/players").json()
        assert players["season"] == "2025-26"
        assert players["season_fallback"] == {
            "requested": DEFAULT_SEASON,
            "used": "2025-26",
        }
        response = client.post(
            "/api/research/breakdown", json={"player_ids": [1], "metrics": ["pts"]}
        )
        assert response.status_code == 200, response.text
        assert response.json()["scope"]["season"] == "2025-26"
        assert response.json()["notice"].startswith(
            f"No matching data is available for {DEFAULT_SEASON}"
        )
        calls.clear()
        response = client.post(
            "/api/research/breakdown",
            json={"season": DEFAULT_SEASON, "player_ids": [1]},
        )
        assert response.status_code == 422
        assert calls == [DEFAULT_SEASON]
    finally:
        main.app.dependency_overrides.clear()
