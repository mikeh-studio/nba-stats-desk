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

            def ask(question=QUESTION, transport="json", **query):
                model.responses.create = lambda **kwargs: SimpleNamespace(
                    output_text=json.dumps(raw_plan(**query))
                )
                response = client.post(
                    "/api/agent/ask"
                    + ("/stream" if transport == "sse" else "")
                    + "?season=2024-25",
                    json={"question": question},
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
        {"season": "2023-24"},
        {"season_type": "Playoffs"},
        {"start_date": "2025-04-10"},
        {"as_of": "2025-04-14"},
        {"window": "season_to_date"},
        {"home_away": "HOME"},
        {"opponent_abbr": "AAA"},
        {"operation": "game_log"},
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
