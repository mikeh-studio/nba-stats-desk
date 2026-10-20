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
        self.requests = []

    def create(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        schema = kwargs.get("text", {}).get("format", {}).get("schema", {})
        result = (
            {"request": self.result}
            if "request" in schema.get("properties", {})
            else self.result
        )
        return SimpleNamespace(output_text=json.dumps(result), usage=None)


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
        replace(_settings(), season="2025-26", agent_teammate_study_path=str(path)),
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
@pytest.mark.parametrize(
    "narration",
    [
        "Other Player averaged +0.88 rebounds when Focal Player played, proving a causal effect.",
        "Focal Player had significantly higher assists when Other Player participated than when Out.",
        None,
    ],
)
def test_public_ask_http_and_stream_handlers(tmp_path, stream, narration):
    from app import main as api
    from fastapi.testclient import TestClient

    agent, model_client = setup(tmp_path, response={"answer": narration})
    if narration is None:
        from app.agent.teammate_ask import SCHEMA
        from jsonschema import validate

        del model_client.result["answer"]
        validate(model_client.result, SCHEMA)
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
        if narration is not None:
            assert narration not in response.text
        assert result["study_status"] == "answered"
        assert result["tables"][0]["rows"][2][1] == "+0.88"
        profile = result["player_profile"]
        assert profile["player"]["player_name"] == "Focal Player"
        assert profile["player"]["headshot_url"].endswith("/1.png")
        assert profile["profile_url"] == "/players/1"
        assert profile["scopeLabel"] == (
            "2025-26 regular season · 2025-10-22 through 2025-12-31"
        )
        assert profile["player"]["team_abbr"] is None
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
    assert result["research_status"] == "tested"
    assert result["tables"] == []
    assert client.calls == 0


@pytest.mark.parametrize("replacement", [False, True])
def test_partial_catalog_preserves_other_legacy_pairs(
    tmp_path, monkeypatch, replacement
):
    from app.research_studies import PAIRS, pending

    data = study()
    data["scope"].update(
        focal_player_id=1630552,
        focal_player_name="Jalen Johnson",
        teammate_id=1629027,
        teammate_name="Trae Young",
    )
    agent, client = setup(tmp_path, data=data)
    agent.settings = replace(agent.settings, research_studies_path="catalog.json")
    entries = [pending(p) for p in PAIRS]
    entries[0]["scope"] = {"season": "2025-26"}
    if replacement:
        entries[1]["scope"] = {"season": "2025-26"}
    monkeypatch.setattr("app.research_studies.catalog", lambda path: entries)
    monkeypatch.setattr(
        "app.agent.research_ask.answer_research",
        lambda *args: {"research_status": "tested"},
    )
    result = agent.answer(
        "Using the teammate study, how did Jalen Johnson's assists differ when Trae Young was out from 2025-10-22 to 2025-12-31?"
    )
    if replacement:
        assert result["research_status"] == "tested"
        assert result["tables"] == []
        assert client.calls == 0
    else:
        assert result["study_status"] == "answered"
        assert client.calls == 1


@pytest.mark.parametrize("difference,direction", [(0.88, "higher"), (-0.88, "lower")])
def test_adjusted_direction_and_claim_binding(tmp_path, difference, direction):
    import hashlib

    data = study()
    data["statistics"]["adjusted"]["difference"] = difference
    agent, _ = setup(tmp_path, data=data)
    result = agent.answer("Review teammate study")
    assert result["answer"].startswith(
        f"Focal Player's adjusted assists were {direction} in the group where Other Player was reported Out with no appearance versus participated ({difference:+.2f} assists per game, Out minus participated)."
    )
    claims = result["study_claims"]
    assert claims["version"] == "teammate_study_claims/1"
    assert (
        claims["evidence_id"]
        == "sha256:"
        + hashlib.sha256(
            json.dumps(
                data, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
    )
    assert len(claims["claims"]) == 9
    for claim in claims["claims"]:
        value = data
        for key in claim["source_path"].split("."):
            value = value[key]
        assert claim["value"] == value
        assert claim["evidence_id"] == claims["evidence_id"]
        assert claim["scope"] == data["scope"]
        assert claim["study_id"] == "fixture"
        assert claim["origin"] == "imported_frozen_estimate"
    assert claims["claims"][6]["unit"] == "assists per game"
    assert (
        claims["claims"][6]["contrast"]
        == "reported_out_no_appearance_minus_participated"
    )


@pytest.mark.parametrize(
    "path,invalid",
    [
        ("statistics.original_raw_difference", None),
        ("statistics.original_raw_difference", float("nan")),
        ("statistics.complete_case_raw_difference", True),
        ("statistics.adjusted.difference", float("inf")),
        ("statistics.adjusted.nominal_p", -0.1),
        ("statistics.adjusted.nominal_p", 1.1),
        ("statistics.adjusted.nominal_p", "0.4"),
        ("statistics.adjusted.nominal_cluster_95_ci", [1, -1]),
        ("statistics.adjusted.nominal_cluster_95_ci", [None, 2]),
        ("statistics.adjusted.nominal_cluster_95_ci", [float("nan"), 2]),
        ("statistics.adjusted.nominal_cluster_95_ci", [2]),
        ("statistics.original_played_n", 0),
        ("statistics.original_out_n", -1),
        ("statistics.adjusted.played_n", 10),
        ("statistics.adjusted.out_n", 0.5),
        ("statistics.validated_significance", True),
        ("statistics.difference_direction", "participated minus Out"),
        ("statistics.difference_direction", None),
        ("scope.focal_player_id", True),
        ("scope.teammate_id", 1),
        ("scope.focal_player_name", ""),
        ("scope.start", "2026-01-01"),
        ("scope.end", "2027-01-01"),
    ],
)
def test_invalid_imported_study_fails_before_model(tmp_path, path, invalid):
    data = study()
    target = data
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = invalid
    agent, client = setup(tmp_path, data=data)
    with pytest.raises(AgentExecutionError):
        agent.answer("Review teammate study")
    assert client.calls == 0


def test_missing_imported_value_never_defaults_to_zero(tmp_path):
    data = study()
    del data["statistics"]["adjusted"]["difference"]
    agent, client = setup(tmp_path, data=data)
    with pytest.raises(AgentExecutionError):
        agent.answer("Review teammate study")
    assert client.calls == 0


@pytest.mark.parametrize(
    "response", [{"scope_matches": "true"}, {"focal_player_id": True}]
)
def test_out_of_schema_scope_is_rejected(tmp_path, response):
    agent, _ = setup(tmp_path, response=response)
    assert agent.answer("Review teammate study")["study_status"] == "unavailable"


@pytest.mark.parametrize("stream", [False, True])
def test_actual_published_case_endpoint_and_mutations(tmp_path, stream):
    import copy
    from pathlib import Path

    from app import main as api
    from fastapi.testclient import TestClient
    from scripts.ask_eval.grading import grade

    fixture = Path(__file__).parent / "fixtures" / "ask"
    data = json.loads((fixture / "source.json").read_text())["study"]
    case = next(
        c
        for c in json.loads((fixture / "cases.json").read_text())["cases"]
        if c["id"] == "research_published_study"
    )
    turn = case["turns"][0]
    agent, model = setup(tmp_path, data=data, response=turn["study_response"])
    settings = replace(
        agent.settings,
        season="2024-25",
        performance_cache_prewarm_enabled=False,
        agent_rate_limit_per_minute=0,
        agent_history_enabled=False,
    )
    previous = dict(api.app.dependency_overrides)
    api.app.dependency_overrides.update(
        {
            api.get_settings: lambda: settings,
            api.get_repository: lambda: SimpleNamespace(settings=settings),
            api.get_agent_client: lambda: model,
        }
    )
    try:
        with TestClient(api.app) as client:
            response = client.post(
                "/api/agent/ask" + ("/stream" if stream else ""),
                json={"question": turn["question"]},
            )
        assert response.status_code == 200
        if stream:
            events = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            result = next(e["payload"] for e in events if e.get("type") == "final")
            assert (
                "".join(e["delta"] for e in events if e.get("type") == "answer_delta")
                == result["answer"]
            )
        else:
            result = response.json()
        assert all(c["passed"] for c in grade(result, turn["checks"]))
        claim_mutations = [("study_claims.evidence_id", "sha256:wrong")]
        for i in range(9):
            for key, value in [
                ("value", 999),
                ("source_path", "statistics.wrong_field"),
                ("display_paths", ["tables.0.rows.0.0"]),
                ("evidence_id", "sha256:wrong"),
                ("unit", "points per game"),
                ("scope", {}),
                ("qualifier", "validated"),
                ("contrast_basis", "declared"),
            ]:
                claim_mutations.append((f"study_claims.claims.{i}.{key}", value))
        for path, wrong in claim_mutations + [
            ("answer", result["answer"].replace("higher", "lower")),
            ("answer", result["answer"].replace("assists", "rebounds")),
            ("answer", result["answer"].replace("Avery Example", "Blair Sample")),
            ("answer", result["answer"].replace("no validated", "validated")),
            ("study_claims.claims.6.scope.focal_player_id", 202),
            ("study_claims.claims.6.scope.start", "2025-04-11"),
            ("study_claims.claims.6.metric", "pts"),
            ("study_claims.claims.6.unit", "points per game"),
            ("study_claims.claims.6.contrast", "participated_minus_out"),
            ("study_claims.claims.6.source_path", "statistics.original_raw_difference"),
            ("tables.0.title", "Rebounds per game — 2025-04-10 to 2025-04-15"),
            ("tables.0.rows.2.0", "Adjusted difference (played minus Out)"),
            ("tables.0.rows.2.1", "+2.00"),
            ("charts", [{"title": "Invented points chart", "value": 1.5}]),
        ]:
            altered = copy.deepcopy(result)
            parts = path.split(".")
            target = altered
            for part in parts[:-1]:
                target = target[int(part)] if isinstance(target, list) else target[part]
            key = int(parts[-1]) if isinstance(target, list) else parts[-1]
            target[key] = wrong
            checks = grade(altered, turn["checks"])
            assert any(not c["passed"] for c in checks), path
            assert all(
                c["passed"] for c in checks if c["path"] in ("study_status", "study_id")
            ), path
    finally:
        api.app.dependency_overrides.clear()
        api.app.dependency_overrides.update(previous)


@pytest.mark.parametrize("value", [0, -0.0, 0.004, -0.003])
def test_zero_display_does_not_claim_absence_of_association(tmp_path, value):
    data = study()
    data["statistics"]["adjusted"]["difference"] = value
    agent, _ = setup(tmp_path, data=data)
    result = agent.answer("Review teammate study")
    assert result["answer"].startswith(
        "Focal Player's adjusted point estimate rounds to 0.00 assists per game for Other Player reported Out with no appearance minus participated. This is not evidence of no association."
    )
    assert "unchanged" not in result["answer"]
    assert result["tables"][0]["rows"][2][1] == "+0.00"
    assert result["study_claims"]["claims"][6]["value"] == value


@pytest.mark.parametrize(
    "word", ["playoff", "playoffs", "postseason", "post-season", "post season"]
)
def test_literal_phase_guard_never_calls_model(tmp_path, word):
    agent, client = setup(tmp_path)
    result = agent.answer(f"Review teammate study in the {word}")
    assert result["study_status"] == "unavailable"
    assert not result["tables"] and client.calls == 0


@pytest.mark.parametrize("date_value", [20251022, "20251022", "2025-W43-3", None])
def test_malformed_dates_are_value_errors_and_no_natural_route_fallback(
    tmp_path, date_value
):
    from app.agent.teammate_ask import load_study

    data = study()
    data["scope"]["start"] = date_value
    agent, client = setup(tmp_path, data=data)
    with pytest.raises(ValueError):
        load_study(agent.settings.agent_teammate_study_path)
    for catalog in (None, "configured-catalog.json"):
        agent.settings = replace(agent.settings, research_studies_path=catalog)
        with pytest.raises(AgentExecutionError, match="Teammate study request failed"):
            agent.answer(
                "How did Focal Player's assists change when Other Player was out?"
            )
    assert client.calls == 0


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        "null",
        '{"version":true}',
        '{"version":1,"scope":[]}',
        '{"unused":NaN}',
        '{"unused":Infinity}',
    ],
)
def test_bad_json_shapes_normalize_to_value_error_and_never_call_model(tmp_path, raw):
    from pathlib import Path

    from app.agent.teammate_ask import load_study

    agent, client = setup(tmp_path)
    Path(agent.settings.agent_teammate_study_path).write_text(raw)
    with pytest.raises(ValueError):
        load_study(agent.settings.agent_teammate_study_path)
    with pytest.raises(AgentExecutionError, match="Teammate study request failed"):
        agent.answer("How did Focal Player's assists change when Other Player was out?")
    assert client.calls == 0


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_nonfinite_unused_fields_rejected_before_provider(tmp_path, value):
    data = study()
    data["unused"] = value
    agent, client = setup(tmp_path, data=data)
    with pytest.raises(AgentExecutionError):
        agent.answer("Review teammate study")
    assert client.calls == 0


def test_conforming_scope_only_response_and_no_private_scope_leak(tmp_path):
    from app.agent.teammate_ask import SCHEMA
    from jsonschema import validate

    data = study()
    data["scope"]["private_note"] = "fixture private context must not leak"
    agent, client = setup(tmp_path, data=data)
    del client.result["answer"]
    validate(client.result, SCHEMA)
    result = agent.answer("Review teammate study")
    assert result["study_status"] == "answered"
    assert "private_note" not in json.dumps(result)
    evidence = json.loads(
        client.requests[0]["input"][0]["content"].removeprefix("EVIDENCE: ")
    )
    assert set(evidence) == {"scope"}
    assert "private_note" not in evidence["scope"]
    assert "statistics" not in json.dumps(evidence)


def test_declared_contrast_and_v1_default_are_distinguishable(tmp_path):
    data = study()
    data["statistics"]["difference_direction"] = (
        "Reported Out minus participated; positive means higher focal-player assists in the Out group"
    )
    agent, _ = setup(tmp_path, data=data)
    result = agent.answer("Review teammate study")
    assert all(
        c["contrast_basis"] == "declared" for c in result["study_claims"]["claims"]
    )


@pytest.mark.parametrize("value", [0, 0.00001, 0.0009])
def test_tiny_nominal_p_never_displays_as_zero(tmp_path, value):
    data = study()
    data["statistics"]["adjusted"]["nominal_p"] = value
    agent, _ = setup(tmp_path, data=data)
    result = agent.answer("Review teammate study")
    assert result["tables"][0]["rows"][4][1] == "<0.001"
    assert result["study_claims"]["claims"][8]["value"] == value
