"""Latest-first Ask scope never replaces an explicit or invalid source scope."""

from types import SimpleNamespace

import pytest
from app.agent.semantic_serving import (
    has_time_scope,
    requested_seasons,
    season_candidates,
)
from app.agent.semantics import SemanticError
from app.agent.service import StatsAgent
from tests.test_research_api import research_client as _research_client
from tests.test_semantic_migration import Model, Warehouse, settings

research_client = _research_client


@pytest.mark.parametrize(
    "question",
    [
        "PPG in 2024-25",
        "PPG during March",
        "PPG last season",
        "PPG this season",
        "PPG from 2024-11-01 to 2024-12-01",
        "PPG in the last five games",
    ],
)
def test_explicit_periods_are_not_fallback_requests(question):
    assert has_time_scope(question)
    assert len(season_candidates(question, "2025-26")) == 1


def test_relative_season_and_descending_candidates():
    assert requested_seasons("PPG last season", "2025-26") == ["2024-25"]
    assert season_candidates("Regular Player PPG", "2025-26") == [
        "2025-26",
        "2024-25",
        "2023-24",
    ]


def test_governed_answer_uses_latest_available_and_discloses_it():
    warehouse = Warehouse()
    agent = StatsAgent(
        settings("2025-26"),
        SimpleNamespace(),
        client=Model(),
        semantic_warehouse=warehouse,
    )
    result = agent.answer("Regular Player PPG")
    assert result["status"] == "ok"
    assert result["season_fallback"] == {"requested": "2025-26", "used": "2024-25"}
    assert result["semantic_evidence"]["scope"]["season"] == "2024-25"
    assert "most recent available season" in result["answer"]
    assert warehouse.calls[-1] == ["2024-25"]
    assert ["2023-24"] not in warehouse.calls


def test_explicit_missing_season_never_falls_back():
    warehouse = Warehouse(SemanticError("unsupported_coverage", "Missing season"))
    model = Model()
    agent = StatsAgent(
        settings("2025-26"),
        SimpleNamespace(),
        client=model,
        semantic_warehouse=warehouse,
    )
    result = agent.answer("Regular Player PPG in 2025-26")
    assert result["status"] == "unsupported_coverage"
    assert warehouse.calls == [["2025-26"]]
    assert not model.calls


@pytest.mark.parametrize(
    "code",
    ["access_denied", "invalid_evidence", "incomplete_evidence", "duplicate_grain"],
)
def test_source_errors_do_not_trigger_older_data(code):
    warehouse = Warehouse(SemanticError(code, "Source failed"))
    agent = StatsAgent(
        settings("2025-26"),
        SimpleNamespace(),
        client=Model(),
        semantic_warehouse=warehouse,
    )
    result = agent.answer("Regular Player PPG")
    assert result["status"] == code
    assert warehouse.calls == [["2025-26"]]


def test_previous_season_is_explicit_for_governed_ask():
    warehouse = Warehouse()
    agent = StatsAgent(
        settings("2025-26"),
        SimpleNamespace(),
        client=Model(),
        semantic_warehouse=warehouse,
    )
    result = agent.answer("Regular Player PPG last season")
    assert result["status"] == "ok"
    assert result["semantic_evidence"]["scope"]["season"] == "2024-25"
    assert "season_fallback" not in result


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_study_fallback_and_explicit_scope_through_http(
    research_client, monkeypatch, explicit, stream
):
    import json

    from app.agent import research_ask
    from app.research import ResearchQuery
    from app.research_studies import PAIRS, pending
    from tests.test_research_insights import metric

    client, _, planner = research_client
    pair = PAIRS[0]
    study = pending(pair)
    study["scope"] = dict(
        season="2024-25",
        start="2024-11-01",
        end="2024-11-30",
        window="full_season",
        phase="Both",
    )
    study["metrics"][0] = metric()
    monkeypatch.setattr(research_ask, "catalog", lambda path: [study])
    query = ResearchQuery(player_ids=[pair["player_id"]], metrics=["pts"]).model_dump(
        mode="json"
    )
    query["teammate_id"] = pair["teammate_id"]
    planner.result = dict(
        kind="study", query=query, pair_id=pair["pair_id"], message=""
    )
    question = "How did LeBron play while Luka was out" + (
        " in 2025-26" if explicit else ""
    )
    response = client.post(
        "/api/agent/ask" + ("/stream" if stream else ""), json={"question": question}
    )
    assert response.status_code == 200
    if stream:
        events = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        payload = next(e["payload"] for e in events if e.get("type") == "final")
    else:
        payload = response.json()
    if explicit:
        assert payload["research_status"] == "unsupported"
        assert "You requested 2025-26" in payload["answer"]
        assert "2024-11-01 through 2024-11-30" in payload["answer"]
        assert not payload["tables"]
    else:
        assert payload["season_fallback"]["used"] == "2024-25"
        assert payload["research_scope"]["season"] == "2024-25"
        assert payload["research_scope"]["start"] == "2024-11-01"
        assert "Using 2024-25" in payload["answer"]
        assert payload["tables"]


def test_study_answer_survives_missing_season_boxscores(research_client, monkeypatch):
    from app.agent import research_ask
    from app.research import ResearchQuery
    from app.research_studies import PAIRS, pending
    from tests.test_research_insights import metric

    client, _, planner = research_client
    pair = PAIRS[0]
    study = pending(pair)
    study["scope"] = dict(
        season="2025-26",
        start="2025-11-01",
        end="2025-11-30",
        window="full_season",
        phase="Both",
    )
    study["metrics"][0] = metric()
    monkeypatch.setattr(research_ask, "catalog", lambda path: [study])

    def missing(*args):
        raise SemanticError("unsupported_coverage", "No box scores")

    monkeypatch.setattr(research_ask, "load_research_evidence", missing)
    query = ResearchQuery(player_ids=[pair["player_id"]], metrics=["pts"]).model_dump(
        mode="json"
    )
    query["teammate_id"] = pair["teammate_id"]
    planner.result = dict(
        kind="study", query=query, pair_id=pair["pair_id"], message=""
    )
    payload = client.post(
        "/api/agent/ask", json={"question": "LeBron without Luka"}
    ).json()
    assert payload["research_scope"]["season"] == "2025-26"
    assert payload["tables"]
    assert "season_fallback" not in payload


def test_missing_phase_checks_older_season_without_another_model_call():
    class PhaseWarehouse(Warehouse):
        def load(self, seasons):
            if seasons == ["2025-26"]:
                self.calls.append(seasons)
                from tests.test_research import evidence

                current = evidence()
                for row in current.rows:
                    row["player_name"] = {1: "Regular Player", 2: "Small Sample"}[
                        row["player_id"]
                    ]
                return {}, current
            return super().load(seasons)

    source = PhaseWarehouse()
    model = Model(season_type="Playoffs")
    result = StatsAgent(
        settings("2025-26"), SimpleNamespace(), client=model, semantic_warehouse=source
    ).answer("Regular Player playoff PPG")
    assert result["status"] == "ok"
    assert result["season_fallback"]["used"] == "2024-25"
    assert result["semantic_evidence"]["scope"]["season_type"] == "Playoffs"
    assert len(model.calls) == 1


def test_breakdown_preserves_filters_when_falling_back(research_client, monkeypatch):
    from dataclasses import replace

    from app.agent import research_ask
    from tests.test_research import evidence

    client, query, planner = research_client
    current = evidence()
    current = replace(current, rows=[dict(r, home_away="away") for r in current.rows])
    archived = replace(
        current,
        rows=[
            dict(
                r,
                season="2024-25",
                game_date=r["game_date"].replace("2025", "2024"),
                home_away="home",
            )
            for r in current.rows
        ],
        covered_scopes=frozenset({("2024-25", "Regular Season")}),
        data_through={("2024-25", "Regular Season"): "2024-11-07"},
    )
    calls = []

    def load(settings, repo, season):
        calls.append(season)
        return current if season == "2025-26" else archived

    monkeypatch.setattr(research_ask, "load_research_evidence", load)
    result = client.post(
        "/api/agent/ask", json={"question": "Research Player 1 and Player 2 at home"}
    ).json()
    assert result["research_scope"]["home_away"] == "home"
    assert result["research_scope"]["season"] == "2024-25"
    assert result["season_fallback"]["used"] == "2024-25"
    assert calls == ["2025-26", "2024-25"]
    assert planner.calls == 1
