"""Ask integration must preserve scope and server-authored evidence tables."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.agent.service import AgentExecutionError, StatsAgent
from app.agent.teammate_ask import wants_study
from tests.test_agent_service import _settings


def study():
    return {
        "version": 1,
        "study_id": "fixture",
        "claim_level": "exploratory_adjusted_association",
        "scope": {
            "focal_player_id": 1,
            "focal_player_name": "Focal Player",
            "teammate_id": 2,
            "teammate_name": "Other Player",
            "season": "2025-26",
            "phase": "Regular Season",
            "start": "2025-10-22",
            "end": "2025-12-31",
            "metric": "ast",
        },
        "statistics": {
            "original_raw_difference": 1.28,
            "original_played_n": 9,
            "original_out_n": 22,
            "complete_case_raw_difference": -1.24,
            "adjusted": {
                "difference": 0.88,
                "played_n": 5,
                "out_n": 21,
                "nominal_cluster_95_ci": [-4.46, 6.21],
                "nominal_p": 0.672,
            },
            "validated_significance": None,
        },
        "limitations": ["Exploratory association, not causal."],
    }


class Client:
    def __init__(self, result):
        self.result = result
        self.responses = self
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(output_text=json.dumps(self.result), usage=None)


def setup(tmp_path, data=None, response=None):
    data = data or study()
    path = tmp_path / "study.json"
    path.write_text(json.dumps(data))
    result = {
        k: v
        for k, v in data["scope"].items()
        if k in ("focal_player_id", "teammate_id", "season", "start", "end", "metric")
    }
    result.update(scope_matches=True, answer="The difference is uncertain.")
    result.update(response or {})
    client = Client(result)
    agent = StatsAgent(
        replace(_settings(), agent_teammate_study_path=str(path)),
        SimpleNamespace(),
        client=client,
    )
    return agent, client


def test_actual_service_returns_governed_table_and_scope_notice(tmp_path):
    agent, client = setup(tmp_path)
    result = agent.answer("Using the teammate study, is the difference significant?")
    assert result["study_status"] == "answered"
    assert result["tables"][0]["rows"][2][1] == "+0.88"
    assert "no validated statistical-significance or causal claim" in result["answer"]
    assert result["evidence_scope"]["start"] == "2025-10-22"
    assert client.calls == 1


@pytest.mark.parametrize(
    "question",
    [
        "Teammate study from 2025-12-01 to 2025-12-31",
        "Teammate study in 2024-25",
    ],
)
def test_literal_scope_mismatch_does_not_call_model(tmp_path, question):
    agent, client = setup(tmp_path)
    result = agent.answer(question)
    assert result["study_status"] == "unavailable"
    assert not result["tables"]
    assert client.calls == 0


@pytest.mark.parametrize(
    "response",
    [
        {"focal_player_id": 2, "teammate_id": 1},
        {"scope_matches": False},
        {"metric": "pts"},
        {"start": "2025-12-01"},
    ],
)
def test_model_cannot_apply_mismatched_evidence(tmp_path, response):
    agent, _ = setup(tmp_path, response=response)
    result = agent.answer("Review teammate study")
    assert result["study_status"] == "unavailable"
    assert "+0.88" not in result["answer"]


def test_no_artifact_returns_unavailable(tmp_path):
    agent = StatsAgent(_settings(), SimpleNamespace(), client=Client({}))
    assert agent.answer("Review teammate study")["study_status"] == "unavailable"


def test_invalid_claim_level_fails_closed(tmp_path):
    data = study()
    data["claim_level"] = "causal"
    agent, client = setup(tmp_path, data=data)
    with pytest.raises(AgentExecutionError):
        agent.answer("Review teammate study")
    assert client.calls == 0


def test_regular_stat_request_keeps_existing_route():
    assert not wants_study("How many assists did Jalen Johnson average?")


def test_named_pair_natural_question_routes_to_study(tmp_path):
    agent, client = setup(tmp_path)
    result = agent.answer(
        "How did Focal Player's assists change when Other Player was out?"
    )
    assert result["study_status"] == "answered"
    assert client.calls == 1


def test_significance_followup_retains_study_route(tmp_path):
    from app.agent.conversation import InMemoryConversationStore

    agent, client = setup(tmp_path)
    agent.conversation_store = InMemoryConversationStore()
    agent.answer("Review teammate study", conversation_id="study-followup")
    assert (
        agent.answer(
            "Is that statistically significant?", conversation_id="study-followup"
        )["study_status"]
        == "answered"
    )
    assert client.calls == 2


@pytest.mark.parametrize("stream", [False, True])
def test_public_ask_http_and_stream_handlers(tmp_path, stream):
    from app import main as api
    from fastapi.testclient import TestClient

    agent, model_client = setup(tmp_path)
    settings = replace(
        agent.settings,
        performance_cache_prewarm_enabled=False,
        agent_rate_limit_per_minute=100,
        agent_history_enabled=False,
    )
    previous = dict(api.app.dependency_overrides)
    api.app.dependency_overrides[api.get_settings] = lambda: settings
    api.app.dependency_overrides[api.get_repository] = lambda: SimpleNamespace(
        settings=settings
    )
    api.app.dependency_overrides[api.get_agent_client] = lambda: model_client
    try:
        with TestClient(api.app) as client:
            response = client.post(
                "/api/agent/ask" + ("/stream" if stream else ""),
                json={"question": "Review teammate study"},
            )
        assert response.status_code == 200
        if stream:
            events = [
                json.loads(line.removeprefix("data: "))
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            result = next(e["payload"] for e in events if e.get("type") == "final")
            assert any(e.get("type") == "answer_delta" for e in events)
        else:
            result = response.json()
        assert result["study_status"] == "answered"
        assert result["tables"][0]["rows"][2][1] == "+0.88"
    finally:
        api.app.dependency_overrides.clear()
        api.app.dependency_overrides.update(previous)


def test_registered_pair_legacy_study_remains_reachable(tmp_path):
    data = study()
    data["scope"].update(
        focal_player_id=1630552,
        focal_player_name="Jalen Johnson",
        teammate_id=1629027,
        teammate_name="Trae Young",
    )
    agent, client = setup(tmp_path, data=data)
    result = agent.answer(
        "Using the teammate study, how did Jalen Johnson's assists differ when Trae Young was out from 2025-10-22 to 2025-12-31?"
    )
    assert result["study_status"] == "answered"
    assert result["tables"]
    assert client.calls == 1


def test_configured_research_catalog_keeps_route_precedence(tmp_path, monkeypatch):
    data = study()
    data["scope"].update(focal_player_name="Jalen Johnson", teammate_name="Trae Young")
    agent, client = setup(tmp_path, data=data)
    agent.settings = replace(
        agent.settings, research_studies_path="configured-catalog.json"
    )
    monkeypatch.setattr(
        "app.agent.research_ask.answer_research",
        lambda *args: {"research_status": "tested"},
    )
    result = agent.answer(
        "How did Jalen Johnson's assists differ when Trae Young was out?"
    )
    assert result == {"research_status": "tested"}
    assert client.calls == 0
