"""Independent endpoint holdouts for the dated points product default."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.agent.execution import ExecutionFailure
from app.agent.semantic_planner import plan_question
from app.agent.semantics import SemanticError
from fastapi.testclient import TestClient
from scripts.ask_eval.fixtures import frozen_repository, load_inputs
from tests.support.semantic import settings


def client_for(plan):
    return SimpleNamespace(
        responses=SimpleNamespace(
            create=lambda **kwargs: SimpleNamespace(output_text=json.dumps(plan))
        )
    )


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
                "aggregation": "unspecified",
                "aggregation_source": "omitted",
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
        teams=("AAA", "BBB"),
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
    for row in source["rows"]:
        if row["player_id"] == 202 and row["game_date"] == "2025-04-12":
            row["fga"] = 0  # Zero attempts do not erase seven recorded points.
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
def test_endpoint_holdout_uses_valid_appearances_and_discloses(endpoint, transport):
    payload = endpoint(transport=transport)
    assert payload["status"] == "ok", payload
    row = payload["semantic_evidence"]["rows"][0]
    assert (row["value"], row["valid_games"], row["observed_games"]) == (3.5, 2, 3)
    policy = payload["query_plan"]["aggregation_policy"]
    assert policy["original_aggregation"] == "unspecified"
    assert policy["chosen_aggregation"] == "average"
    assert "points per game" in " ".join(payload["assumptions"])
    assert "aggregation_source" not in payload["query_plan"]["queries"][0]


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "term,aggregation,value",
    [("total points", "total", 7), ("average points", "average", 3.5)],
)
def test_explicit_aggregation_has_no_default(
    endpoint, transport, term, aggregation, value
):
    payload = endpoint(
        QUESTION.replace("points", term),
        transport,
        aggregation=aggregation,
        aggregation_source="explicit",
    )
    assert payload["semantic_evidence"]["rows"][0]["value"] == value
    assert "aggregation_policy" not in payload["query_plan"]


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "aggregation,source",
    [
        ("unspecified", "omitted"),
        ("average", "inherited"),
        ("average", "planner_selected"),
        ("average", "explicit"),
    ],
)
def test_current_total_cannot_be_erased(endpoint, transport, aggregation, source):
    payload = endpoint(
        QUESTION.replace("points", "total points"),
        transport,
        aggregation=aggregation,
        aggregation_source=source,
    )
    assert payload["status"] != "ok" and not payload["tables"]


@pytest.mark.parametrize(
    "change",
    [
        {"metric": "ast"},
        {"operation": "game_log"},
        {"season": "2023-24"},
        {"season_type": "Playoffs"},
        {"start_date": "2025-04-10"},
        {"as_of": "2025-04-14"},
        {"window": "last_n_days", "n": 3},
        {"home_away": "HOME"},
        {"opponent_abbr": "AAA"},
        {"player_name": "resolved_player_999"},
    ],
)
def test_raw_mismatches_fail_before_literal_scope_can_overwrite(change):
    with pytest.raises((SemanticError, ExecutionFailure)):
        planned(**change)


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "suffix",
    [
        " on Tuesdays",
        " in wins",
        " excluding overtime",
        " and predict tomorrow",
        " with at least 20 points",
        " using a rolling average",
        " and explain causal effects",
    ],
)
def test_canonical_unsupported_modifiers_withhold(endpoint, transport, suffix):
    payload = endpoint(QUESTION + suffix, transport)
    assert payload.get("status") != "ok" and not payload["tables"], payload


@pytest.mark.parametrize(
    "question",
    [
        QUESTION.upper() + "?",
        QUESTION.replace("Blair Sample", "B Sample"),
        "Please show me " + QUESTION,
        QUESTION.replace("points", "points scored"),
        "What were Blair Sample’s points from 2025-04-11 to 2025-04-13 in 2024-25?",
    ],
)
def test_literal_variants(question):
    assert planned(question)["queries"][0]["aggregation"] == "average"


def test_advisory_provenance_is_optional_but_invalid_values_cannot_execute():
    raw = raw_plan()
    raw["queries"][0].pop("aggregation_source")
    result = plan_question(
        client_for(raw),
        model="test",
        question=QUESTION,
        selected_season="2024-25",
        players=PLAYERS,
        teams=("AAA", "BBB"),
    )
    assert result["queries"][0]["aggregation"] == "average"
    assert result["aggregation_policy"]["model_aggregation_source"] == "omitted"
    with pytest.raises((SemanticError, ExecutionFailure)):
        planned(QUESTION.replace("points", "assists"), metric="ast")
    for value in ("ratio", None, [], "sum"):
        with pytest.raises((SemanticError, ExecutionFailure)):
            planned(aggregation=value)


@pytest.mark.parametrize(
    "question,change",
    [
        (
            QUESTION.replace("points", "assists"),
            {"metric": "ast", "aggregation": "average"},
        ),
        (
            QUESTION.replace("points", "points game log"),
            {"operation": "game_log", "aggregation": "total"},
        ),
        (
            QUESTION.replace("points", "games played"),
            {"metric": "gp", "aggregation": "total"},
        ),
        (
            QUESTION.replace("points", "FG%"),
            {"metric": "fg_pct", "aggregation": "ratio"},
        ),
    ],
)
def test_noneligible_model_selections_keep_existing_semantics(question, change):
    result = planned(question, aggregation_source="planner_selected", **change)
    assert result["queries"][0]["aggregation"] == change["aggregation"]
    assert "aggregation_policy" not in result


def test_partial_followup_inherits_total_and_complete_restatement_can_default():
    context = {
        "players": PLAYERS,
        "metrics": ["pts"],
        "scope": {"aggregation": "total"},
    }
    question = QUESTION.replace("Blair Sample ", "")
    result = planned(
        question, context, aggregation="total", aggregation_source="inherited"
    )
    assert result["queries"][0]["aggregation"] == "total"
    with pytest.raises((SemanticError, ExecutionFailure)):
        planned(question, context)
    assert (
        planned(context=context)["aggregation_policy"]["chosen_aggregation"]
        == "average"
    )
    result = planned(
        question.replace("points", "average points"),
        context,
        aggregation="average",
        aggregation_source="explicit",
    )
    assert result["queries"][0]["aggregation"] == "average"


def test_calendar_phase_and_sparse_scope():
    result = planned(
        "Blair Sample points in April 2025 in 2024-25",
        start_date="2025-04-01",
        as_of="2025-04-30",
    )
    assert result["aggregation_policy"]["request_scope"]["start_date"] == "2025-04-01"
    assert (
        planned(
            QUESTION + " including regular season and playoffs", season_type="Both"
        )["queries"][0]["season_type"]
        == "Both"
    )
    assert planned(start_date=None)["queries"][0]["start_date"] == "2025-04-11"


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_endpoint_two_turns_inherit_then_explicit_override(endpoint, transport):
    from uuid import uuid4

    cid = str(uuid4())
    first = endpoint(
        QUESTION.replace("points", "total points"),
        transport,
        cid,
        aggregation="total",
        aggregation_source="explicit",
    )
    assert first["status"] == "ok", first
    second = endpoint(
        QUESTION.replace("Blair Sample", "his"),
        transport,
        cid,
        aggregation="total",
        aggregation_source="inherited",
    )
    assert second["semantic_evidence"]["rows"][0]["value"] == 7, second
    third = endpoint(
        QUESTION.replace("Blair Sample points", "his average points"),
        transport,
        cid,
        aggregation="average",
        aggregation_source="explicit",
    )
    assert third["semantic_evidence"]["rows"][0]["value"] == 3.5, third
    assert "aggregation_policy" not in third["query_plan"]


def test_independent_two_four_six_acceptance():
    from app.agent.semantic_planner import execute_plan
    from scripts.ask_eval.fixtures import FrozenWarehouse

    source = copy.deepcopy(SOURCE)
    for row in source["rows"]:
        if row["player_id"] == 202:
            row["pts"] = {"2025-04-11": 2, "2025-04-12": 4, "2025-04-13": 6}.get(
                row["game_date"], 23
            )
    _, evidence = FrozenWarehouse(source, "complete").load(["2024-25"])
    assert (
        execute_plan(planned(), evidence, PLAYERS)["evidence"]["rows"][0]["value"] == 4
    )
    total = planned(
        QUESTION.replace("points", "total points"),
        aggregation="total",
        aggregation_source="explicit",
    )
    assert execute_plan(total, evidence, PLAYERS)["evidence"]["rows"][0]["value"] == 12


@pytest.mark.parametrize(
    "metric,aggregation,phrase",
    [
        ("gp", "total", "games played"),
        ("fg_pct", "ratio", "FG%"),
        ("ts_pct", "ratio", "average TS%"),
    ],
)
def test_followup_metric_conventions_do_not_inherit_incompatible_aggregation(
    metric, aggregation, phrase
):
    context = {
        "players": PLAYERS,
        "metrics": ["pts"],
        "scope": {"aggregation": "average"},
    }
    result = planned(
        QUESTION.replace("Blair Sample points", phrase),
        context,
        metric=metric,
        aggregation=aggregation,
        aggregation_source="explicit"
        if phrase.startswith("average")
        else "planner_selected",
    )
    assert result["queries"][0]["aggregation"] == aggregation
    assert "aggregation_policy" not in result


@pytest.mark.parametrize(
    "suffix",
    [
        " from 2025-04-11 through 2025-04-13",
        " in April 2025",
        " at home and away games",
        " regular season and playoffs",
    ],
)
def test_competing_single_summary_scopes_withhold(suffix):
    with pytest.raises((SemanticError, ExecutionFailure)):
        planned(QUESTION + suffix)


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


def test_duplicated_calendar_scope_withholds():
    with pytest.raises((SemanticError, ExecutionFailure)):
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


def test_sparse_plan_and_null_fields_inherit_literal_scope():
    raw = raw_plan()
    for key in ("window", "start_date", "as_of", "season_type"):
        raw["queries"][0].pop(key)
    plan = plan_question(
        client_for(raw),
        model="test",
        question=QUESTION,
        selected_season="2024-25",
        players=PLAYERS,
        teams=("AAA", "BBB"),
    )
    assert plan["aggregation_policy"]["chosen_aggregation"] == "average"
    assert plan["queries"][0]["start_date"] == "2025-04-11"
    # The controlled adapter materializes a complete typed Scope before the host.
    assert plan["aggregation_policy"]["derived_scope_fields"] == []
    assert planned(start_date=None)["queries"][0]["start_date"] == "2025-04-11"


@pytest.fixture
def conversation():
    from app.agent.conversation import InMemoryConversationStore
    from app.agent.semantic_answer import SemanticAsk
    from scripts.ask_eval.fixtures import FrozenWarehouse

    source = copy.deepcopy(SOURCE)
    store = InMemoryConversationStore()
    service = SemanticAsk(settings(), FrozenWarehouse(source, "complete"), store)

    def ask(question, selected_player=None, **query):
        if "aggregation" in query:
            query.setdefault("aggregation_source", "explicit")
        elif "total points" in question:
            query.update(aggregation="total", aggregation_source="explicit")
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
        current["query_plan"]["aggregation_policy"]["original_aggregation"]
        == "unspecified"
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
    # Identity preflight now precedes planning; the resumed original question
    # keeps its explicit average while recording the normal dated-query policy.
    assert (
        selected["query_plan"]["aggregation_policy"]["chosen_aggregation"] == "average"
    )
    assert (
        selected["query_plan"]["aggregation_policy"]["model_aggregation_source"]
        == "explicit"
    )
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


def test_new_dated_assists_request_does_not_inherit_prior_points_total():
    result = planned(
        QUESTION.replace("points", "assists"),
        {"players": PLAYERS, "metrics": ["pts"], "scope": {"aggregation": "total"}},
        metric="ast",
        aggregation="average",
        aggregation_source="planner_selected",
    )
    assert result["queries"][0]["aggregation"] == "average"


def test_explicit_average_multimonth_range_keeps_existing_plan():
    result = planned(
        "Blair Sample points per game from March 2025 through April 2025 in 2024-25",
        aggregation="average",
        aggregation_source="explicit",
        start_date="2025-03-01",
        as_of="2025-04-30",
    )
    assert result["queries"][0]["aggregation"] == "average"
    assert "aggregation_policy" not in result


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_games_played_counts_null_points_appearance(endpoint, transport):
    payload = endpoint(
        QUESTION.replace("points", "games played"),
        transport,
        metric="gp",
        aggregation="total",
        aggregation_source="planner_selected",
    )
    assert payload["semantic_evidence"]["rows"][0]["value"] == 3


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_empty_dated_scope_does_not_fabricate_zero(endpoint, transport):
    payload = endpoint(
        QUESTION.replace(
            "2025-04-11 through 2025-04-13", "2025-04-09 through 2025-04-09"
        ),
        transport,
        start_date="2025-04-09",
        as_of="2025-04-09",
    )
    assert payload["status"] == "no_observations", payload
    assert not payload["semantic_evidence"]["rows"]


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("aggregation,value", [("total", 7), ("average", 3.5)])
def test_explicit_opponent_filter_retains_supported_aggregation(
    endpoint, transport, aggregation, value
):
    payload = endpoint(
        QUESTION.replace("points", f"{aggregation} points against BBB"),
        transport,
        aggregation=aggregation,
        aggregation_source="explicit",
        opponent_abbr="BBB",
    )
    assert payload["status"] == "ok", payload
    assert payload["semantic_evidence"]["rows"][0]["value"] == value
    assert payload["query_plan"]["queries"][0]["opponent_abbr"] == "BBB"
    assert "aggregation_policy" not in payload["query_plan"]


def test_explicit_total_filter_and_scope_contradictions():
    question = QUESTION.replace("points", "total points against AAA")
    result = planned(
        question,
        aggregation="total",
        aggregation_source="explicit",
        opponent_abbr="AAA",
    )
    assert result["queries"][0]["opponent_abbr"] == "AAA"
    with pytest.raises((SemanticError, ExecutionFailure)):
        planned(
            question,
            aggregation="total",
            aggregation_source="explicit",
            opponent_abbr="AAA",
            as_of="2025-04-14",
        )


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("kind", ["phase", "aggregation"])
def test_recognized_negations_cannot_assert_their_opposite(endpoint, transport, kind):
    if kind == "phase":
        # Otherwise valid playoff observations: refusal cannot pass merely from an empty cohort.
        question = (
            QUESTION.replace(
                "2025-04-11 through 2025-04-13", "2025-04-16 through 2025-04-17"
            )
            + " not playoffs"
        )
        query = dict(
            start_date="2025-04-16", as_of="2025-04-17", season_type="Playoffs"
        )
    else:
        question = QUESTION.replace("points", "points, not total")
        query = dict(aggregation="total", aggregation_source="explicit")
    payload = endpoint(question, transport, **query)
    assert payload["status"] in {
        "clarification_required",
        "unsupported_scope",
        "unsupported",
    }, payload
    assert not payload["tables"] and not payload.get("semantic_evidence")


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_complete_season_assists_request_does_not_inherit_total(endpoint, transport):
    from uuid import uuid4

    cid = str(uuid4())
    first = endpoint(
        QUESTION.replace("points", "total points"),
        transport,
        cid,
        aggregation="total",
        aggregation_source="explicit",
    )
    assert first["status"] == "ok", first
    payload = endpoint(
        "Blair Sample assists in 2024-25",
        transport,
        cid,
        metric="ast",
        aggregation="average",
        aggregation_source="planner_selected",
        window="season_to_date",
        start_date=None,
        as_of=None,
    )
    assert payload["status"] == "ok", payload
    assert payload["query_plan"]["queries"][0]["aggregation"] == "average"
    assert payload["semantic_evidence"]["rows"][0]["value"] == 4.5
    assert "aggregation_policy" not in payload["query_plan"]


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "phrase",
    ["How many points did Blair Sample score", "How many PTS did Blair Sample score"],
)
def test_quantity_language_without_aggregation_clarifies(endpoint, transport, phrase):
    payload = endpoint(QUESTION.replace("Blair Sample points", phrase), transport)
    assert payload["status"] == "clarification_required", payload
    assert not payload["tables"] and not payload.get("semantic_evidence")


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_explicit_per_game_quantity_keeps_average(endpoint, transport):
    payload = endpoint(
        QUESTION.replace(
            "Blair Sample points", "How many points per game did Blair Sample score"
        ),
        transport,
        aggregation="average",
        aggregation_source="explicit",
    )
    assert payload["status"] == "ok", payload
    assert payload["semantic_evidence"]["rows"][0]["value"] == 3.5
    assert "aggregation_policy" not in payload["query_plan"]


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("opponent", [None, "BBB"])
def test_omitted_opponent_filter_clarifies_even_when_plan_drops_it(
    endpoint, transport, opponent
):
    payload = endpoint(
        QUESTION.replace("points", "points against BBB"),
        transport,
        opponent_abbr=opponent,
    )
    assert payload["status"] == "clarification_required", payload
    assert not payload["tables"] and not payload.get("semantic_evidence")


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "aggregation,source", [("unspecified", "omitted"), ("total", "explicit")]
)
def test_canonical_road_scope_fills_omissions_but_rejects_conflicts(
    endpoint, transport, aggregation, source
):
    question = QUESTION.replace(
        "points",
        ("total points" if source == "explicit" else "points") + " in road games",
    )
    payload = endpoint(
        question,
        transport,
        aggregation=aggregation,
        aggregation_source=source,
        home_away="AWAY",
    )
    assert payload["status"] == "ok", payload
    assert payload["semantic_evidence"]["rows"][0]["value"] == 7
    assert payload["query_plan"]["queries"][0]["home_away"] == "AWAY"
    filled = endpoint(
        question, transport, aggregation=aggregation, aggregation_source=source
    )
    assert filled["status"] == "ok"
    assert filled["query_plan"]["queries"][0]["home_away"] == "AWAY"
    assert filled["semantic_evidence"]["rows"][0]["value"] == 7
    bad = endpoint(
        question,
        transport,
        aggregation=aggregation,
        aggregation_source=source,
        home_away="HOME",
    )
    assert bad["status"] != "ok" and not bad["tables"]


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_explicit_average_mixed_phase_needs_affirmative_combination(
    endpoint, transport
):
    payload = endpoint(
        QUESTION.replace("points", "average points regular season and playoffs"),
        transport,
        aggregation="average",
        aggregation_source="explicit",
        season_type="Both",
    )
    assert payload["status"] == "clarification_required", payload
    assert not payload["tables"] and not payload.get("semantic_evidence")


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_per_game_chart_retains_game_log_count_convention(endpoint, transport):
    payload = endpoint(
        QUESTION.replace("points", "points per game chart"),
        transport,
        operation="game_log",
        aggregation="total",
        aggregation_source="planner_selected",
    )
    assert payload["status"] == "ok", payload
    assert payload["query_plan"]["queries"][0]["aggregation"] == "total"
    assert payload["query_plan"]["queries"][0]["operation"] == "game_log"
    assert "aggregation_policy" not in payload["query_plan"]


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_ratio_ranking_total_attempt_threshold_remains_ratio(endpoint, transport):
    payload = endpoint(
        "Rank players by FG% with at least 1 total FGA and minimum 1 game in 2024-25",
        transport,
        player_name=None,
        metric="fg_pct",
        operation="rank",
        min_attempts=1,
        min_games=1,
        window="season_to_date",
        start_date=None,
        as_of=None,
        aggregation="ratio",
        aggregation_source="planner_selected",
    )
    assert payload["status"] == "ok", payload
    assert payload["query_plan"]["queries"][0]["aggregation"] == "ratio"
    assert payload["query_plan"]["queries"][0]["min_attempts"] == 1


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_total_rebounds_stat_name_can_be_per_game(endpoint, transport):
    payload = endpoint(
        QUESTION.replace("points", "total rebounds per game"),
        transport,
        metric="reb",
        aggregation="average",
        aggregation_source="explicit",
    )
    assert payload["status"] == "ok", payload
    assert payload["query_plan"]["queries"][0]["aggregation"] == "average"


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("kind", ["gp", "game_log"])
def test_prior_convention_total_is_not_inherited_as_user_intent(
    endpoint, transport, kind
):
    from uuid import uuid4

    cid = str(uuid4())
    first = endpoint(
        QUESTION.replace(
            "points", "games played" if kind == "gp" else "points game log"
        ),
        transport,
        cid,
        metric="gp" if kind == "gp" else "pts",
        operation="summary" if kind == "gp" else "game_log",
        aggregation="total",
        aggregation_source="planner_selected",
    )
    assert first["status"] == "ok", first
    second = endpoint(
        "What about points?",
        transport,
        cid,
        aggregation="average",
        aggregation_source="planner_selected",
    )
    assert second["status"] == "ok", second
    assert second["semantic_evidence"]["rows"][0]["value"] == 3.5
    assert "aggregation_policy" not in second["query_plan"]


# Public orchestration tests use authored plans through the provider contract.
pytestmark = pytest.mark.usefixtures("scripted_intelligence")
