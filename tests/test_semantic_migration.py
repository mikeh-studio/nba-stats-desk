import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.agent.conversation import InMemoryConversationStore
from app.agent.semantic_serving import (
    requested_seasons,
    source_players,
)
from app.agent.semantics import Evidence, SemanticError
from app.agent.service import StatsAgent
from app.config import Settings
from app.repository import BigQueryWarehouseRepository
from fastapi.testclient import TestClient
from tests.semantic_intent_fixtures import migrate_legacy_plan


def settings(season="2024-25"):
    return Settings(
        project_id="test-project",
        gold_dataset="nba_gold",
        metadata_dataset="nba_metadata",
        freshness_threshold_hours=36,
        max_search_results=12,
        season=season,
        openai_api_key="fixture-key",
        agent_rate_limit_per_minute=0,
        agent_rate_limit_daily=0,
    )


class Warehouse:
    def __init__(self, error=None):
        self.error = error
        self.calls = []
        fixture = json.loads(
            (Path(__file__).parent / "fixtures/semantics/cases.json").read_text()
        )
        self.rows = [
            dict(
                r,
                player_name={1: "Regular Player", 2: "Small Sample", 3: "Third Player"}[
                    r["player_id"]
                ],
            )
            for r in fixture["rows"]
        ]
        self.coverage = fixture["coverage"]

    def load(self, seasons):
        self.calls.append(seasons)
        if self.error:
            raise self.error
        scopes = frozenset((s, p) for s, p, _ in self.coverage if s in seasons)
        evidence = Evidence(
            [r for r in self.rows if r["season"] in seasons],
            scopes,
            "fixture/games",
            "frozen-fixture",
            {(s, p): d for s, p, d in self.coverage if s in seasons},
            complete=True,
        )
        return {
            "capture": {
                "query_id": "fixture-job",
                "bytes_processed": 123,
                "query_count": 1,
            }
        }, evidence


class Model:
    def __init__(self, metric="pts", **query):
        self.metric = metric
        self.query = query
        self.calls = []
        self.responses = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = json.loads(kwargs["input"][0]["content"])
        self.content = content
        queries = [
            {
                "metric": self.metric,
                "season": content["selected_season"],
                "aggregation": "ratio" if self.metric == "fg_pct" else "average",
                "player_name": "resolved_player_2"
                if self.metric == "fg_pct"
                else "resolved_player_1",
                **self.query,
            }
        ]
        return SimpleNamespace(
            output_text=json.dumps(
                {
                    "plan": migrate_legacy_plan(
                        {"status": "query", "queries": queries, "message": ""}, kwargs
                    )
                }
            )
        )


def test_stats_agent_uses_governed_math_and_existing_answer_shape():
    source = Warehouse()
    client = Model("fg_pct")
    agent = StatsAgent(
        settings(), SimpleNamespace(), client=client, semantic_warehouse=source
    )
    payload = agent.answer("Small Sample FG% this season")
    assert payload["status"] == "ok"
    assert payload["tables"][0]["rows"][0][1] == "10.0%"
    assert payload["semantic_evidence"]["rows"][0]["numerator"] == 1
    assert payload["semantic_evidence"]["rows"][0]["denominator"] == 10
    assert payload["metric_definitions"][0]["key"] == "fg_pct"
    assert payload["source_query"]["query_id"] == "fixture-job"
    assert source.calls == [["2024-25"]]


def test_bigquery_repository_automatically_enables_semantic_path():
    repo = BigQueryWarehouseRepository(settings(), client=SimpleNamespace())
    assert StatsAgent(settings(), repo, client=Model()).semantic_agent is not None


def test_weighted_fantasy_alias_is_canonicalized_in_response():
    payload = StatsAgent(
        settings(),
        SimpleNamespace(),
        client=Model("Fantasy Score"),
        semantic_warehouse=Warehouse(),
    ).answer("Regular Player average Fantasy Score")
    assert payload["semantic_evidence"]["metric"]["key"] == "fantasy_proxy_weighted"
    assert payload["semantic_evidence"]["rows"][0]["value"] == pytest.approx(19.9)
    assert payload["query_plan"]["queries"][0]["metric"] == "fantasy_proxy_weighted"


@pytest.mark.parametrize(
    "question, expected",
    [
        ("PPG as of 2025-02-14", ["2024-25"]),
        ("Compare 2023-2024 and 2024-25", ["2023-24", "2024-25"]),
    ],
)
def test_season_extraction_does_not_misread_iso_dates(question, expected):
    assert requested_seasons(question, "2024-25") == expected


def test_permission_error_is_not_a_legacy_or_current_season_fallback():
    source = Warehouse(SemanticError("access_denied", "Access denied"))
    model = Model()
    payload = StatsAgent(
        settings(), SimpleNamespace(), client=model, semantic_warehouse=source
    ).answer("Regular Player PPG")
    assert payload["status"] == "access_denied" and not model.calls
    assert payload["semantic_evidence"] is None


def test_missing_attempt_clarification_can_resume_with_threshold():
    model = Model(
        "fg_pct", operation="rank", player_name=None, min_attempts=1, min_games=1
    )
    store = InMemoryConversationStore()
    agent = StatsAgent(
        settings(),
        SimpleNamespace(),
        client=model,
        semantic_warehouse=Warehouse(),
        conversation_store=store,
    )
    first = agent.answer("Who leads FG%?", conversation_id="test")
    assert first["status"] == "clarification_required" and not model.calls
    second = agent.answer("At least 1 FGA and one game", conversation_id="test")
    assert second["status"] == "ok"
    assert "Clarification:" in model.content["question"]
    assert store.get_pending_clarification("test") is None


def test_identity_discovery_includes_small_samples():
    _, evidence = Warehouse().load(["2024-25"])
    assert 2 in {p["player_id"] for p in source_players(evidence)}


def test_api_default_bigquery_path_returns_governed_answer(monkeypatch):
    from app.main import app, get_agent_client, get_repository, get_settings

    repo = BigQueryWarehouseRepository(settings(), client=SimpleNamespace())
    repo._governed_warehouse = Warehouse()
    old = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: settings,
            get_repository: lambda: repo,
            get_agent_client: lambda: Model("fg_pct"),
        }
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/agent/ask?season=2024-25",
                json={"question": "Small Sample FG% this season"},
            )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["semantic_evidence"]["metric"]["key"] == "fg_pct"
        assert payload["tables"][0]["rows"][0][1] == "10.0%"
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)


def test_selected_season_override_queries_only_the_requested_archive():
    source = Warehouse()
    agent = StatsAgent(
        settings(), SimpleNamespace(), client=Model(), semantic_warehouse=source
    )
    payload = agent.answer("Regular Player PPG in 2023-24")
    assert source.calls == [["2023-24"]]
    assert payload["semantic_evidence"]["scope"]["season"] == "2023-24"
    assert payload["semantic_evidence"]["rows"][0]["value"] == 40


def test_api_stream_returns_governed_final_event():
    from app.main import app, get_agent_client, get_repository, get_settings

    repo = BigQueryWarehouseRepository(settings(), client=SimpleNamespace())
    repo._governed_warehouse = Warehouse()
    old = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: settings,
            get_repository: lambda: repo,
            get_agent_client: lambda: Model("fg_pct"),
        }
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/agent/ask/stream?season=2024-25",
                json={"question": "Small Sample FG% this season"},
            )
        assert response.status_code == 200
        assert "governed_metrics" in response.text
        assert '"numerator": 1.0' in response.text
        assert "10.0%" in response.text
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)


def test_snapshot_cache_retries_invalid_capture_and_expires(monkeypatch):
    from app.agent import semantic_serving

    now = [0]
    calls = []
    snapshots = iter([{"valid": False}, {"valid": True}, {"valid": True}])

    def capture(seasons):
        calls.append(seasons)
        return next(snapshots)

    def validate(snapshot):
        if not snapshot["valid"]:
            raise SemanticError("incomplete_source", "Invalid capture")
        return "validated"

    monkeypatch.setattr(semantic_serving, "monotonic", lambda: now[0])
    monkeypatch.setattr(semantic_serving, "snapshot_evidence", validate)
    warehouse = semantic_serving.SemanticWarehouse(
        SimpleNamespace(capture=capture), ttl_seconds=10
    )
    with pytest.raises(SemanticError):
        warehouse.load(["2024-25"])
    first, evidence = warehouse.load(["2024-25"])
    assert evidence == "validated"
    assert warehouse.load(["2024-25"])[0] is first
    assert len(calls) == 2
    now[0] = 11
    assert warehouse.load(["2024-25"])[0] is not first
    assert len(calls) == 3


def test_game_log_answer_and_chart_share_observations():
    payload = StatsAgent(
        settings(),
        SimpleNamespace(),
        client=Model("Fantasy Score", operation="game_log", window="last_n_games", n=2),
        semantic_warehouse=Warehouse(),
    ).answer("Regular Player Fantasy Score game log for the last 2 appearances")
    assert payload["status"] == "ok"
    assert [r[1] for r in payload["tables"][0]["rows"]] == ["006", "007"]
    assert [p["y"] for p in payload["charts"][0]["series"][0]["points"]] == [19.9, 19.9]
    assert payload["metric_definitions"][0]["key"] == "fantasy_proxy_weighted"


def test_game_log_unknown_value_is_visible_and_chart_is_withheld():
    warehouse = Warehouse()
    warehouse.rows[0]["pts"] = None
    payload = StatsAgent(
        settings(),
        SimpleNamespace(),
        client=Model(operation="game_log"),
        semantic_warehouse=warehouse,
    ).answer("Regular Player points game log")
    assert payload["tables"][0]["rows"][0][-1] == "Unavailable"
    assert payload["charts"] == []
    assert any("Missing values are not zero" in a for a in payload["assumptions"])


@pytest.mark.parametrize("date", ["2025/02/14", "2025-02-14", "2025–02–14"])
def test_dates_are_not_seasons(date):
    assert requested_seasons(f"LeBron PPG as of {date}", "2024-25") == ["2024-25"]
    assert requested_seasons(f"2023/24 PPG as of {date}", "2024-25") == ["2023-24"]
