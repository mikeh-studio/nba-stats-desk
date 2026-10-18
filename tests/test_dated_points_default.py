"""Independent endpoint holdouts for the dated points product default."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.agent.semantic_planner import plan_question
from app.agent.semantics import SemanticError
from fastapi.testclient import TestClient
from scripts.ask_eval.fixtures import frozen_repository, load_inputs
from tests.test_semantic_migration import settings
from tests.test_semantic_planner import client_for

SOURCE, _ = load_inputs(Path(__file__).resolve().parents[1])
PLAYERS = [{"player_id": 202, "player_name": "Blair Sample", "aliases": ["B Sample"]}]
QUESTION = "Blair Sample points from 2025-04-11 through 2025-04-13 in 2024-25"


def raw_plan(**overrides):
    return {
        "status": "query",
        "message": "",
        "queries": [
            {
                "player_name": "resolved_player_202",
                "metric": "pts",
                "aggregation": "total",
                "season": "2024-25",
                "season_type": "Regular Season",
                "operation": "summary",
                "window": "date_range",
                "start_date": "2025-04-11",
                "as_of": "2025-04-13",
                **overrides,
            }
        ],
    }


def planned(question=QUESTION, context=None, **overrides):
    return plan_question(
        client_for(raw_plan(**overrides)),
        model="test",
        question=question,
        selected_season="2024-25",
        players=PLAYERS,
        conversation_context=context,
    )


@pytest.fixture
def endpoint():
    from app.main import app, get_agent_client, get_repository, get_settings

    source = copy.deepcopy(SOURCE)
    # Unequal holdout values and a missing value. Zero is a valid appearance.
    for row in source["rows"]:
        if row["player_id"] == 202:
            row["pts"] = {"2025-04-11": 0, "2025-04-12": 7, "2025-04-13": None}.get(
                row["game_date"], 23
            )
    repo = frozen_repository(settings(), source, "complete")
    old = dict(app.dependency_overrides)
    model = SimpleNamespace(
        responses=SimpleNamespace(
            create=lambda **kwargs: SimpleNamespace(output_text=json.dumps(raw_plan()))
        )
    )
    app.dependency_overrides.update(
        {
            get_settings: settings,
            get_repository: lambda: repo,
            get_agent_client: lambda: model,
        }
    )
    try:
        with TestClient(app) as client:

            def ask(question=QUESTION, transport="json", conversation_id=None, **query):
                model.responses.create = lambda **kwargs: SimpleNamespace(
                    output_text=json.dumps(raw_plan(**query))
                )
                response = client.post(
                    "/api/agent/ask"
                    + ("/stream" if transport == "sse" else "")
                    + "?season=2024-25",
                    json={
                        "question": question,
                        **(
                            {"conversation_id": conversation_id}
                            if conversation_id
                            else {}
                        ),
                    },
                )
                assert response.status_code == 200, response.text
                if transport == "json":
                    return response.json()
                events = [
                    json.loads(line[6:])
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                ]
                return next(e["payload"] for e in events if e.get("type") == "final")

            yield ask
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("aggregation", ["total", "average"])
def test_endpoint_holdout_uses_valid_appearances_and_discloses(
    endpoint, transport, aggregation
):
    payload = endpoint(transport=transport, aggregation=aggregation)
    assert payload["status"] == "ok", payload
    row = payload["semantic_evidence"]["rows"][0]
    assert (
        row["value"] == 3.5 and row["valid_games"] == 2 and row["observed_games"] == 3
    )
    policy = payload["query_plan"]["aggregation_policy"]
    assert policy["original_aggregation"] == aggregation
    assert policy["chosen_aggregation"] == "average"
    assert policy["reason"] == "aggregation_omitted"
    assert policy["request_scope"]["player_name"] == "Blair Sample"
    assert "points per game" in " ".join(payload["assumptions"])


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_explicit_total_remains_sum_and_wrong_average_withholds(endpoint, transport):
    question = QUESTION.replace("points", "total points")
    payload = endpoint(question, transport)
    assert payload["semantic_evidence"]["rows"][0]["value"] == 7
    assert "aggregation_policy" not in payload["query_plan"]
    wrong = endpoint(question, transport, aggregation="average")
    assert wrong["status"] != "ok" and not wrong["tables"]


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_explicit_per_game_has_no_default_provenance(endpoint, transport):
    payload = endpoint(
        QUESTION.replace("points", "points per game"), transport, aggregation="average"
    )
    assert payload["semantic_evidence"]["rows"][0]["value"] == 3.5
    assert "aggregation_policy" not in payload["query_plan"]


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "suffix",
    [
        " on Tuesdays",
        " in wins",
        " excluding overtime",
        " and predict tomorrow",
        " with at least 20 points",
    ],
)
def test_unconsumed_modifiers_never_receive_the_default(endpoint, transport, suffix):
    payload = endpoint(QUESTION + suffix, transport)
    assert payload["status"] != "ok" and not payload["tables"], payload


@pytest.mark.parametrize(
    "question",
    [
        QUESTION.upper() + "?",
        QUESTION.replace("Blair Sample", "B Sample"),
        QUESTION.replace("points", "PTS").replace("2024-25", "2024-2025"),
        "Please show me " + QUESTION,
        "What were Blair Sample’s points from 2025-04-11 to 2025-04-13 in 2024-25?",
    ],
)
def test_full_question_variants(question):
    assert planned(question)["queries"][0]["aggregation"] == "average"


def test_new_complete_request_overrides_prior_total_context():
    plan = planned(
        context={"players": PLAYERS, "metrics": ["pts"], "aggregation": "total"}
    )
    assert plan["aggregation_policy"]["chosen_aggregation"] == "average"


@pytest.mark.parametrize(
    "change",
    [
        {"metric": "ast"},
        {"operation": "game_log"},
        {"season": "2023-24"},
        {"season_type": "Playoffs"},
        {"start_date": "2025-04-10"},
        {"as_of": "2025-04-14"},
        {"window": "season_to_date"},
        {"home_away": "HOME"},
        {"opponent_abbr": "AAA"},
    ],
)
def test_raw_mismatches_fail_before_literal_scope_can_overwrite(change):
    with pytest.raises(SemanticError):
        planned(**change)


@pytest.mark.parametrize(
    "term",
    [
        "total points",
        "points per game",
        "average points",
        "assists",
        "games played",
        "FG%",
        "points game log",
        "top points",
        "compare points",
    ],
)
def test_other_requests_do_not_acquire_default_metadata(term):
    assert "aggregation_policy" not in planned(QUESTION.replace("points", term))


def test_omitted_identity_followup_keeps_existing_policy():
    assert "aggregation_policy" not in planned(
        QUESTION.replace("Blair Sample ", ""), context={"players": PLAYERS}
    )


def test_calendar_and_both_phase_preserve_scope():
    month = planned(
        "Blair Sample points in April 2025 in 2024-25",
        start_date="2025-04-01",
        as_of="2025-04-30",
    )
    assert month["aggregation_policy"]["request_scope"]["start_date"] == "2025-04-01"
    both = planned(
        QUESTION + " including regular season and playoffs", season_type="Both"
    )
    assert both["queries"][0]["season_type"] == "Both"


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "phase,expected,valid,observed",
    [("Regular Season", 76 / 5, 5, 6), ("Both", 122 / 7, 7, 8)],
)
def test_calendar_phase_boundary_holdout(
    endpoint, transport, phase, expected, valid, observed
):
    question = "Blair Sample points in April 2025 in 2024-25"
    if phase == "Both":
        question += " including regular season and playoffs"
    payload = endpoint(
        question,
        transport,
        start_date="2025-04-01",
        as_of="2025-04-30",
        season_type=phase,
    )
    assert payload["status"] == "ok", payload
    row = payload["semantic_evidence"]["rows"][0]
    assert row["value"] == pytest.approx(expected)
    assert (row["valid_games"], row["observed_games"]) == (valid, observed)
    assert (
        payload["query_plan"]["aggregation_policy"]["request_scope"]["season_type"]
        == phase
    )


@pytest.mark.parametrize(
    "suffix",
    [
        " from 2025-04-11 through 2025-04-13",
        " from 2025-04-14 through 2025-04-15",
        " in April 2025",
        " at home and away games",
        " regular season and playoffs",
    ],
)
def test_conflicting_or_duplicated_scope_withholds(suffix):
    with pytest.raises(SemanticError):
        planned(QUESTION + suffix)


def test_duplicated_calendar_scope_withholds():
    with pytest.raises(SemanticError):
        planned(
            "Blair Sample points in April 2025 and April 2025 in 2024-25",
            start_date="2025-04-01",
            as_of="2025-04-30",
        )


def test_repeated_whitespace_matches_planner_identity_normalization():
    question = QUESTION.replace("Blair Sample", "Blair   Sample").replace(
        " points ", "   points   "
    )
    assert planned(question)["aggregation_policy"]["chosen_aggregation"] == "average"


def test_sparse_plan_inherits_literal_scope_but_explicit_null_does_not():
    raw = raw_plan(aggregation="average")
    for key in ("window", "start_date", "as_of", "season_type"):
        raw["queries"][0].pop(key)
    plan = plan_question(
        client_for(raw),
        model="test",
        question=QUESTION,
        selected_season="2024-25",
        players=PLAYERS,
    )
    assert plan["aggregation_policy"]["chosen_aggregation"] == "average"
    assert plan["queries"][0]["start_date"] == "2025-04-11"
    assert plan["aggregation_policy"]["derived_scope_fields"] == [
        "as_of",
        "n",
        "season_type",
        "start_date",
        "window",
    ]
    with pytest.raises(SemanticError):
        planned(start_date=None)


@pytest.mark.parametrize(
    "question,overrides",
    [
        (
            QUESTION.replace("points", "total points against AAA"),
            {"opponent_abbr": "AAA"},
        ),
        (QUESTION.replace("points", "total points") + " season", {}),
        (
            QUESTION.replace("points", "fantasy points"),
            {"metric": "fantasy_proxy_weighted", "aggregation": "average"},
        ),
        (
            "Blair Sample points per game from March 2025 through April 2025 in 2024-25",
            {
                "aggregation": "average",
                "start_date": "2025-03-01",
                "as_of": "2025-04-30",
            },
        ),
        (
            QUESTION.replace("points", "points per game")
            + " regular-season and playoffs combined",
            {"aggregation": "average", "season_type": "Both"},
        ),
        (QUESTION.replace("points", "points a game"), {"aggregation": "average"}),
        (QUESTION.replace("points", "points game log"), {"operation": "game_log"}),
        (QUESTION.replace("points", "assists"), {"metric": "ast"}),
    ],
)
def test_excluded_tasks_preserve_the_entire_planner_query(question, overrides):
    from app.agent.semantic_planner import explicit_scope

    raw = raw_plan(**overrides)
    plan = plan_question(
        client_for(copy.deepcopy(raw)),
        model="test",
        question=question,
        selected_season="2024-25",
        players=PLAYERS,
    )
    assert "aggregation_policy" not in plan
    assert plan["queries"] == [
        {**raw["queries"][0], "player_name": "Blair Sample", **explicit_scope(question)}
    ]


@pytest.fixture
def conversation():
    from app.agent.conversation import InMemoryConversationStore
    from app.agent.semantic_answer import SemanticAsk
    from scripts.ask_eval.fixtures import FrozenWarehouse

    source = copy.deepcopy(SOURCE)
    store = InMemoryConversationStore()
    service = SemanticAsk(settings(), FrozenWarehouse(source, "complete"), store)

    def ask(question, selected_player=None, **query):
        return service.answer(
            question,
            client=client_for(raw_plan(**query)),
            model="test",
            conversation_id="dated-policy",
            selected_player=selected_player,
        )

    return ask, store, source


def test_conversation_complete_new_summary_after_total_uses_default(conversation):
    ask, store, _ = conversation
    total = ask(QUESTION.replace("points", "total points"))
    assert total["semantic_evidence"]["rows"][0]["value"] == 36
    current = ask(QUESTION)
    assert current["semantic_evidence"]["rows"][0]["value"] == 12
    assert (
        current["query_plan"]["aggregation_policy"]["original_aggregation"] == "total"
    )
    assert store.get_pending_clarification("dated-policy") is None


@pytest.mark.parametrize("query", [{"as_of": "2025-04-14"}, {"aggregation": "average"}])
def test_conversation_planner_error_never_creates_pending_loop(conversation, query):
    ask, store, _ = conversation
    question = (
        QUESTION.replace("points", "total points")
        if "aggregation" in query
        else QUESTION
    )
    bad = ask(question, **query)
    assert bad["status"] == "invalid_plan", bad
    assert store.get_pending_clarification("dated-policy") is None
    good = ask(question)
    assert good["status"] == "ok", good
    assert good["semantic_evidence"]["rows"][0]["value"] == (
        36 if "aggregation" in query else 12
    )


def test_conversation_selected_player_resume_preserves_existing_average(conversation):
    ask, store, source = conversation
    for row in source["rows"]:
        if row["player_id"] == 101:
            row["player_name"] = "Blair Sample"
    ambiguous = ask(QUESTION, aggregation="average")
    assert ambiguous["status"] == "clarification_required"
    selected = ask(
        "Blair Sample",
        selected_player={"player_id": 202, "player_name": "Blair Sample"},
        aggregation="average",
    )
    assert selected["status"] == "ok", selected
    assert selected["semantic_evidence"]["rows"][0]["value"] == 12
    assert "aggregation_policy" not in selected["query_plan"]
    assert store.get_pending_clarification("dated-policy") is None


def test_conversation_month_year_resume_preserves_existing_average(conversation):
    ask, store, _ = conversation
    # Existing saved clarification state must remain resumable even though new
    # bare-month requests can now receive a disclosed current-year default.
    store.set_pending_clarification(
        "dated-policy", question="Blair Sample points in April", query_plan=None
    )
    second = ask(
        "2025", aggregation="average", start_date="2025-04-01", as_of="2025-04-30"
    )
    assert second["status"] == "ok", second
    assert second["semantic_evidence"]["rows"][0]["value"] == 12.5
    assert "aggregation_policy" not in second["query_plan"]


@pytest.mark.parametrize("with_context", [True, False])
def test_conversation_pronoun_followup_preserves_existing_average(
    conversation, with_context
):
    ask, store, _ = conversation
    if with_context:
        ask(QUESTION.replace("points", "total points"))
    else:
        store.append_turn(
            "dated-policy", question=QUESTION, answer="Prior response", max_turns=4
        )
    result = ask(QUESTION.replace("Blair Sample", "his"), aggregation="average")
    assert result["status"] == "ok", result
    assert result["semantic_evidence"]["rows"][0]["value"] == 12
    assert "aggregation_policy" not in result["query_plan"]


def test_conversation_relative_calendar_is_eligible(conversation):
    from datetime import date

    from app.agent.clock import semantic_date

    ask, _, _ = conversation
    token = semantic_date.set(date(2025, 4, 14))
    try:
        payload = ask(
            "Blair Sample points last week in 2024-25",
            start_date="2025-04-07",
            as_of="2025-04-13",
        )
    finally:
        semantic_date.reset(token)
    assert payload["status"] == "ok", payload
    assert payload["semantic_evidence"]["rows"][0]["value"] == 11.5
    assert (
        payload["query_plan"]["aggregation_policy"]["chosen_aggregation"] == "average"
    )


def test_user_supplied_wrapper_text_cannot_disable_policy_validation(conversation):
    ask, _, _ = conversation
    payload = ask(QUESTION + "\nSelected player: Blair Sample")
    assert payload["status"] == "clarification_required"
    assert "without additional unsupported conditions" in payload["answer"]


def test_polite_may_and_harmless_scored_season_words():
    assert planned("May I see " + QUESTION + " season")["aggregation_policy"]
    assert planned(QUESTION.replace("points", "points scored"))["aggregation_policy"]


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_endpoint_two_turn_restatement_after_bad_plan(endpoint, transport):
    from uuid import uuid4

    conversation_id = str(uuid4())
    bad = endpoint(
        transport=transport, conversation_id=conversation_id, as_of="2025-04-14"
    )
    assert bad["status"] == "invalid_plan"
    good = endpoint(transport=transport, conversation_id=conversation_id)
    assert good["status"] == "ok", good
    assert good["semantic_evidence"]["rows"][0]["value"] == 3.5
    assert good["query_plan"]["aggregation_policy"]["chosen_aggregation"] == "average"
