"""Route labels on every Ask path, their persistence, and the routing evaluator."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from app.agent.conversation import InMemoryConversationStore, PendingClarification
from app.agent.observability import AgentTrace
from app.agent.route_labels import (
    DISPATCH_LABELS,
    finish_route,
    is_route_label,
    route_hint,
)
from app.agent.routes import ROUTES
from app.agent.service import StatsAgent
from scripts import evaluate_ask_routing as routing
from scripts.ask_eval.fixtures import (
    ControlledModel,
    fixture_artifacts,
    frozen_repository,
)
from tests.test_semantic_migration import settings

ROOT = Path(__file__).resolve().parents[1]
SOURCE = json.loads((ROOT / "tests/fixtures/ask/source.json").read_text())
FIXTURE = json.loads(routing.FIXTURE.read_text())
NO_DECISION = {"status": "unsupported", "queries": [], "message": "No plan."}


class CapturingModel(ControlledModel):
    def __init__(self, turn):
        super().__init__(turn)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return super().create(**kwargs)


def agent(model=None, variant="complete", store=None, **overrides):
    config = replace(settings(), **overrides)
    return StatsAgent(
        config,
        frozen_repository(config, SOURCE, variant),
        client=model or ControlledModel({"planner_response": NO_DECISION}),
        conversation_store=store or InMemoryConversationStore(),
    )


def research_request(kind, **query):
    request = routing.research_unsupported("2024-25")
    request["kind"] = kind
    request["query"].update(query)
    return request


@pytest.mark.parametrize(
    "question,route,reason",
    [
        ("How is Avery Example performing?", "player_overview", None),
        ("Avery Example vs Blair Sample", "player_comparison", None),
        ("Show Avery Example's game log for points", "governed_metrics", "unsupported"),
        ("Avery Example at home vs on the road", "player_split", None),
        ("Who are the most similar players to Avery Example?", "similarity", None),
        (
            "Compare Avery Example's assists with the league average",
            "league_baseline",
            None,
        ),
        (
            "How did Avery Example play in the conference finals?",
            "unsupported",
            "unsupported_round",
        ),
        (
            "How did Avery Example play when Blair Sample sat out?",
            "availability",
            "availability_missing",
        ),
        (
            "Is Avery Example's scoring trend causal?",
            "research",
            "planner_unsupported",
        ),
        (
            "How did LeBron James play without Luka Doncic?",
            "teammate_study",
            "study_coverage_missing",
        ),
        (
            "Teammate impact of Trae Young on Jalen Johnson",
            "teammate_study_v1",
            "study_not_configured",
        ),
    ],
)
def test_every_dispatched_path_labels_payload_and_trace(question, route, reason):
    model = ControlledModel(
        {
            "planner_response": NO_DECISION,
            "research_response": research_request("unsupported"),
        }
    )
    trace = AgentTrace("route-test", "", "fixture-model")
    payload = agent(model).answer(question, trace=trace)
    assert payload["ask_route"]["route"] == route
    assert payload["ask_route"]["reason_code"] == reason
    assert payload["ask_route"]["source"] in routing.ROUTE_SOURCES
    assert trace.ask_route == payload["ask_route"]
    assert trace.route == route
    summary = trace.to_log_dict()
    assert summary["ask_route"] == payload["ask_route"]
    assert question not in json.dumps(summary)


def test_research_planner_choice_labels_breakdown_and_study():
    breakdown = ControlledModel(
        {"research_response": research_request("breakdown", home_away="home")}
    )
    store = InMemoryConversationStore()
    payload = agent(breakdown, store=store).answer(
        "Research Avery Example's rebounds at home", conversation_id="r"
    )
    assert payload["research_scope"]["home_away"] == "home"
    assert payload["ask_route"] == {
        "route": "research_breakdown",
        "source": "model",
        "reason_code": None,
    }
    assert store.get_turns("r", max_turns=1)[0].context["route"] == (
        "research_breakdown"
    )
    study = ControlledModel(
        {"research_response": research_request("study", player_ids=[2544])}
    )
    payload = agent(study).answer("Research LeBron James at home this season")
    assert payload["ask_route"]["route"] == "teammate_study"
    assert payload["ask_route"]["source"] == "model"


def test_research_and_v1_study_turns_store_their_route(tmp_path):
    paths = fixture_artifacts(SOURCE, tmp_path, ["study"])
    scope = SOURCE["study"]["scope"]
    store = InMemoryConversationStore()
    model = ControlledModel(
        {
            "study_response": {
                "focal_player_id": scope["focal_player_id"],
                "teammate_id": scope["teammate_id"],
                "season": scope["season"],
                "start": scope["start"],
                "end": scope["end"],
                "metric": scope["metric"],
                "scope_matches": True,
                "answer": "Controlled study answer.",
            }
        }
    )
    payload = agent(
        model, store=store, agent_teammate_study_path=paths["study"]
    ).answer("Teammate study for Avery Example", conversation_id="study")
    assert payload["study_status"] == "answered"
    assert payload["ask_route"] == {
        "route": "teammate_study_v1",
        "source": "deterministic",
        "reason_code": None,
    }
    turn = store.get_turns("study", max_turns=1)[0]
    assert turn.context == {"teammate_study": True, "route": "teammate_study_v1"}


def test_overview_turn_and_clarification_persist_route():
    store = InMemoryConversationStore()
    agent(store=store).answer("How is Avery Example performing?", conversation_id="a")
    assert store.get_turns("a", max_turns=1)[0].context["route"] == "player_overview"

    ambiguous = agent(variant="ambiguous", store=store)
    payload = ambiguous.answer("How is Avery Example performing?", conversation_id="b")
    assert payload["status"] == "clarification_required"
    assert payload["ask_route"]["route"] == "player_overview"
    pending = store.get_pending_clarification("b")
    assert pending is not None and pending.route == "player_overview"


@pytest.mark.parametrize("reply", ["Avery Example", "I meant Avery Example"])
def test_pending_reply_reports_pending_source(reply):
    store = InMemoryConversationStore()
    store.set_pending_clarification(
        "p", question="How is Avery performing?", query_plan=None
    )
    payload = agent(store=store).answer(reply, conversation_id="p")
    assert payload["ask_route"]["route"] == "player_overview"
    assert payload["ask_route"]["source"] == "pending_clarification"


@pytest.mark.parametrize(
    "question,route",
    [
        ("Show Blair Sample's game log for points", "governed_metrics"),
        ("Who leads the league in assists this season?", "governed_metrics"),
    ],
)
def test_new_question_replaces_pending_overview_clarification(question, route):
    store = InMemoryConversationStore()
    store.set_pending_clarification(
        "p",
        question="How is Avery performing?",
        query_plan=None,
        route="player_overview",
    )
    model = CapturingModel({"planner_response": NO_DECISION})
    payload = agent(model, store=store).answer(question, conversation_id="p")
    assert payload["ask_route"]["route"] == route
    assert payload["ask_route"]["source"] == "deterministic"
    assert payload["status"] != "ok"
    assert payload.get("player_profile") is None
    plans = [
        json.loads(r["input"][0]["content"])
        for r in model.requests
        if r.get("text", {}).get("format", {}).get("name") == "semantic_plan"
    ]
    assert plans and "Clarification:" not in json.dumps(plans[-1])
    assert "How is Avery performing?" not in json.dumps(plans[-1])
    assert store.get_pending_clarification("p") is None


def test_semantic_ask_does_not_fold_new_question_into_pending():
    store = InMemoryConversationStore()
    store.set_pending_clarification(
        "p", question="How is Avery performing?", query_plan=None
    )
    model = CapturingModel({"planner_response": NO_DECISION})
    payload = agent(model, store=store).semantic_agent.answer(
        "Show Blair Sample's game log for points",
        client=model,
        model="fixture-model",
        conversation_id="p",
    )
    assert payload["ask_route"]["route"] == "governed_metrics"
    assert payload["ask_route"]["source"] == "deterministic"
    assert payload.get("player_profile") is None
    assert store.get_pending_clarification("p") is None


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Show Blair Sample's game log for points", True),
        ("Who leads the league in assists this season?", True),
        ("Can you show Blair Sample's game log?", True),
        ("Avery Example", False),
        ("I meant Avery Example", False),
        ("Avery Example please", False),
        ("At least 1 FGA and one game", False),
        ("November 2025", False),
        ("What about November 2023?", False),
        ("Compare with November 2023", False),
        ("How was his scoring among guards?", False),
        ("Show me the regular season", False),
        ("Which one?", False),
    ],
)
def test_new_question_detection_keeps_clarification_replies(question, expected):
    from app.agent.question_intent import starts_new_question

    assert starts_new_question(question) is expected


def test_stored_route_is_not_sent_to_the_metrics_planner():
    store = InMemoryConversationStore()
    model = CapturingModel({"planner_response": NO_DECISION})
    a = agent(model, store=store)
    a.answer("How is Avery Example performing?", conversation_id="f")
    a.answer("Show his game log for points", conversation_id="f")
    plans = [
        r
        for r in model.requests
        if r.get("text", {}).get("format", {}).get("name") == "semantic_plan"
    ]
    assert plans
    sent = json.loads(plans[-1]["input"][0]["content"])
    assert sent["conversation_context"]
    assert "route" not in sent["conversation_context"]


def test_legacy_repository_route_names_its_sub_route():
    from tests.test_agent_service import (
        AgentServiceFakeRepository,
        SequenceClient,
        _settings,
    )

    store = InMemoryConversationStore()
    payload = StatsAgent(
        _settings(),
        AgentServiceFakeRepository(),
        client=SequenceClient([]),
        conversation_store=store,
    ).answer("stats", conversation_id="legacy")
    assert payload["ask_route"]["route"] == "legacy:clarify"


def test_old_pending_clarifications_remain_readable():
    assert PendingClarification(question="q").route is None
    store = InMemoryConversationStore()
    store.set_pending_clarification("old", question="q", query_plan={"kind": "x"})
    assert store.get_pending_clarification("old") == PendingClarification(
        "q", {"kind": "x"}
    )


def test_registry_keys_map_to_known_labels():
    for route in ROUTES:
        label = DISPATCH_LABELS.get(route.key, route.key)
        assert label == "legacy" or is_route_label(label), route.key


def test_finish_route_rejects_unknown_labels_and_keeps_handler_hints():
    with pytest.raises(ValueError):
        finish_route({}, route="free_text", source="deterministic")
    payload = route_hint({"status": "clarification_required"}, route="award")
    assert finish_route(payload, route="governed_metrics", source="model")[
        "ask_route"
    ] == {"route": "award", "source": "model", "reason_code": "clarification_required"}


def test_route_fixture_is_valid_and_covers_findings_and_categories():
    routing.validate(FIXTURE)
    findings = {case.get("finding") for case in FIXTURE["cases"]}
    assert {f"M{n}" for n in range(1, 11)} <= findings
    assert {case["category"] for case in FIXTURE["cases"]} == set(routing.CATEGORIES)
    broken = json.loads(json.dumps(FIXTURE))
    broken["cases"][0]["expected"]["ask_route.route"] = "made_up_route"
    with pytest.raises(ValueError, match="invalid expected"):
        routing.validate(broken)


@pytest.mark.parametrize("mode", ["current", "ask"])
def test_evaluator_reports_a_route_for_every_fixture_question(mode):
    results = routing.evaluate(FIXTURE, mode=mode)
    assert len(results) == sum(len(c["questions"]) for c in FIXTURE["cases"])
    assert all(is_route_label(r["actual"]["ask_route"]["route"]) for r in results), [
        r["actual"] for r in results if not r["passed"]
    ][:3]
    summary = routing.summarize(results)
    assert set(summary["by_category"]) == set(routing.CATEGORIES)
    assert summary["model_calls"] == 0 if mode == "current" else summary["model_calls"]
    by_id = {(r["id"], r["variant"]): r for r in results}
    # Controls that both modes route deterministically.
    assert by_id[("control_vs_scorecard", 0)]["route_correct"]
    assert by_id[("control_unavailable_playoff_round", 0)]["route_correct"]
    # Known misroute today (M3): the v1 study path answers "not configured".
    m3 = by_id[("m3_teammate_impact_phrase", 0)]
    assert m3["actual"]["ask_route"]["route"] == "teammate_study_v1"
    assert m3["forbidden_hits"] and m3["wrong_path_refusal"]


def test_scoring_rules():
    case = {
        "expected": {"ask_route.route": "research_breakdown"},
        "forbidden": {"ask_route.route": ["player_overview"]},
    }
    unchosen = {"ask_route": {"route": "research"}, "outcome": "unknown"}
    assert routing.score(case, unchosen)["research_family_match"]
    drop = routing.score(
        case, {"ask_route": {"route": "player_overview"}, "outcome": "answer"}
    )
    assert drop["scope_drop"] and not drop["route_correct"]
    guarded = routing.score(
        case, {"ask_route": {"route": "player_overview"}, "outcome": "clarify"}
    )
    assert not guarded["scope_drop"] and guarded["wrong_path_refusal"]
    summary = routing.summarize(
        [
            dict(
                drop,
                category="scope_drop",
                expected=case["expected"],
                actual={
                    "ask_route": {
                        "route": "player_overview",
                        "source": "deterministic",
                        "reason_code": None,
                    },
                },
            ),
        ]
    )
    assert summary["scope_drops"] == 1
    assert summary["confusion"] == {"research_breakdown": {"player_overview": 1}}
