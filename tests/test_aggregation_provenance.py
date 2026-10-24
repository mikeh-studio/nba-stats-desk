"""Independently authored public synthetic controls for server-owned provenance."""

import copy
import json
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from scripts.ask_eval.fixtures import frozen_repository
from tests.test_dated_points_default import SOURCE
from tests.test_semantic_migration import settings

QUESTION = "Casey Orbit points from 2025-03-04 through 2025-03-06 in 2024-25"
LABELS = ["explicit", "inherited", "omitted", "planner_selected", None]


@pytest.fixture
def endpoint_v3():
    from app.main import app, get_agent_client, get_repository, get_settings

    source = copy.deepcopy(SOURCE)
    for row in source["rows"]:
        if row["player_id"] == 202:
            row["player_name"] = "Casey Orbit"
            row["game_date"] = (
                date.fromisoformat(row["game_date"]) - timedelta(days=38)
            ).isoformat()
            row["pts"] = {"2025-03-04": 3, "2025-03-05": 0, "2025-03-06": None}.get(
                row["game_date"], 11
            )
    repo = frozen_repository(settings(), source, "complete")
    model = SimpleNamespace(responses=SimpleNamespace())
    previous = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: settings,
            get_repository: lambda: repo,
            get_agent_client: lambda: model,
        }
    )
    try:
        with TestClient(app) as client:

            def ask(
                question=QUESTION,
                transport="json",
                conversation=None,
                omit_label=False,
                omit_phase=False,
                expect_error=False,
                **overrides,
            ):
                query = dict(
                    metric="pts",
                    season="2024-25",
                    season_type="Regular Season",
                    operation="summary",
                    window="date_range",
                    start_date="2025-03-04",
                    as_of="2025-03-06",
                    player_name="resolved_player_202",
                    aggregation="total",
                    aggregation_source="explicit",
                )
                query.update(overrides)
                if omit_phase:
                    query.pop("season_type")
                if omit_label:
                    query.pop("aggregation_source")
                model.responses.create = lambda **kwargs: SimpleNamespace(
                    output_text=json.dumps(
                        dict(status="query", message="", queries=[query])
                    )
                )
                response = client.post(
                    "/api/agent/ask"
                    + ("/stream" if transport == "sse" else "")
                    + "?season=2024-25",
                    json=dict(
                        question=question, conversation_id=conversation or str(uuid4())
                    ),
                )
                if expect_error:
                    if transport == "json":
                        assert response.status_code == 502
                        assert set(response.json()) == {"detail"}
                    else:
                        assert '"type": "error"' in response.text
                        assert '"type": "final"' not in response.text
                    return {"status": "invalid_provider_output", "tables": []}
                assert response.status_code == 200, response.text
                if transport == "json":
                    return response.json()
                return next(
                    json.loads(line[6:])["payload"]
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                    and json.loads(line[6:]).get("type") == "final"
                )

            yield ask
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("value", ["total", "average", "unspecified"])
@pytest.mark.parametrize("label", LABELS)
@pytest.mark.parametrize(
    "interval",
    [
        "from 2025-03-04 through 2025-03-06",
        "between 2025-03-04 and 2025-03-06",
        "over 2025-03-04 to 2025-03-06",
    ],
)
def test_omitted_aggregation_is_server_owned(
    endpoint_v3, transport, value, label, interval
):
    payload = endpoint_v3(
        QUESTION.replace("from 2025-03-04 through 2025-03-06", interval),
        transport,
        aggregation=value,
        aggregation_source=label,
    )
    assert payload["status"] == "ok", payload
    row = payload["semantic_evidence"]["rows"][0]
    assert (row["value"], row["valid_games"], row["observed_games"]) == (1.5, 2, 3)
    policy = payload["query_plan"]["aggregation_policy"]
    assert policy["original_aggregation"] == value
    assert policy["chosen_aggregation"] == "average"
    resolution = payload["query_plan"]["aggregation_resolution"][0]
    assert resolution["source"] == "omitted_default"
    assert resolution["model_aggregation_source"] == (label or "omitted")
    assert "points per game" in " ".join(payload["assumptions"])


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("label", LABELS)
@pytest.mark.parametrize("value,expected", [("total", 3), ("average", 1.5)])
@pytest.mark.parametrize(
    "interval",
    [
        "from 2025-03-04 through 2025-03-06",
        "between 2025-03-04 and 2025-03-06",
        "over 2025-03-04 to 2025-03-06",
    ],
)
def test_explicit_values_survive_inaccurate_labels(
    endpoint_v3, transport, label, value, expected, interval
):
    question = QUESTION.replace("from 2025-03-04 through 2025-03-06", interval)
    payload = endpoint_v3(
        question.replace("points", value + " points"),
        transport,
        aggregation=value,
        aggregation_source=label,
    )
    assert payload["status"] == "ok", payload
    assert payload["semantic_evidence"]["rows"][0]["value"] == expected
    assert "aggregation_policy" not in payload["query_plan"]
    assert payload["query_plan"]["aggregation_resolution"][0]["source"] == "explicit"
    bad = endpoint_v3(
        question.replace("points", value + " points"),
        transport,
        aggregation="average" if value == "total" else "total",
        aggregation_source=label,
    )
    assert bad["status"] != "ok" and not bad["tables"]


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("value,expected", [("total", 3), ("average", 1.5)])
def test_followup_values_and_fresh_requests_do_not_depend_on_labels(
    endpoint_v3, transport, value, expected
):
    cid = str(uuid4())
    first = endpoint_v3(
        QUESTION.replace("points", value + " points"),
        transport,
        cid,
        aggregation=value,
        aggregation_source="omitted",
    )
    assert first["status"] == "ok", first
    follow = endpoint_v3(
        "What about points?",
        transport,
        cid,
        aggregation=value,
        aggregation_source="explicit",
    )
    assert follow["status"] == "ok", follow
    assert follow["semantic_evidence"]["rows"][0]["value"] == expected
    assert follow["query_plan"]["aggregation_resolution"][0]["source"] == "inherited"
    wrong = endpoint_v3(
        "What about points?",
        transport,
        cid,
        aggregation="average" if value == "total" else "total",
        aggregation_source="inherited",
    )
    assert wrong["status"] != "ok" and not wrong["tables"]
    fresh = endpoint_v3(
        QUESTION, transport, cid, aggregation="total", aggregation_source="inherited"
    )
    assert fresh["semantic_evidence"]["rows"][0]["value"] == 1.5
    assert (
        fresh["query_plan"]["aggregation_resolution"][0]["source"] == "omitted_default"
    )


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "change",
    [
        {"metric": "ast"},
        {"operation": "game_log"},
        {"player_name": "resolved_player_101"},
        {"season_type": "Playoffs"},
        {"start_date": "2025-03-03"},
        {"as_of": "2025-03-07"},
        {"window": "season_to_date"},
        {"opponent_abbr": "BBB"},
        {"aggregation": "ratio"},
        {"home_away": "HOME"},
    ],
)
def test_alternate_scope_cannot_bypass_concrete_validation(
    endpoint_v3, transport, change
):
    payload = endpoint_v3(
        QUESTION.replace(
            "from 2025-03-04 through 2025-03-06", "between 2025-03-04 and 2025-03-06"
        ),
        transport,
        expect_error=change.get("aggregation") == "ratio",
        **change,
    )
    assert payload["status"] != "ok" and not payload["tables"], payload


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "interval",
    [
        "during 2025-03-04 and 2025-03-06",
        "since 2025-03-04",
        "over 2025-03-06 to 2025-03-04",
        "between 2025-03-04 and 2025-03-06 plus 2025-03-07",
        "between 2025-03-04 and 2025-03-06 and between 2025-03-04 and 2025-03-06",
        "from 2025-02-30 to 2025-03-06",
    ],
)
def test_unresolved_competing_or_invalid_dates_cannot_execute_total(
    endpoint_v3, transport, interval
):
    payload = endpoint_v3(
        QUESTION.replace("from 2025-03-04 through 2025-03-06", interval), transport
    )
    assert payload["status"] != "ok" and not payload["tables"], payload


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_missing_label_is_not_execution_authority(endpoint_v3, transport):
    payload = endpoint_v3(transport=transport, omit_label=True)
    assert payload["status"] == "ok", payload
    assert (
        payload["query_plan"]["aggregation_policy"]["original_aggregation"] == "total"
    )
    assert (
        payload["query_plan"]["aggregation_resolution"][0]["model_aggregation_source"]
        == "omitted"
    )


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_count_chart_metadata_is_convention_not_explicit_total(endpoint_v3, transport):
    payload = endpoint_v3(
        QUESTION.replace("points", "points per game chart"),
        transport,
        operation="game_log",
        aggregation="total",
        aggregation_source="explicit",
    )
    assert payload["status"] == "ok", payload
    assert (
        payload["query_plan"]["aggregation_resolution"][0]["source"]
        == "metric_convention"
    )


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_known_since_month_average_remains_outside_closed_interval_default(
    endpoint_v3, transport
):
    question = "Casey Orbit points since March 2025"
    payload = endpoint_v3(
        question,
        transport,
        start_date="2025-03-01",
        aggregation="average",
        aggregation_source="explicit",
    )
    assert payload["status"] == "ok", payload
    assert payload["semantic_evidence"]["rows"][0]["value"] == pytest.approx(14 / 3)
    assert "aggregation_policy" not in payload["query_plan"]
    assert (
        payload["query_plan"]["aggregation_resolution"][0]["source"]
        == "legacy_average_compatibility"
    )
    for query in [
        {"aggregation": "total"},
        {"aggregation": "unspecified"},
        {"aggregation": "average", "start_date": "2025-03-02"},
    ]:
        bad = endpoint_v3(question, transport, **{"start_date": "2025-03-01", **query})
        assert bad["status"] != "ok" and not bad["tables"]


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "suffix",
    [" plus 2025-03-07", " and since April 2025", " over 2025-03-04 to 2025-03-06"],
)
def test_since_month_cannot_hide_extra_anchors(endpoint_v3, transport, suffix):
    payload = endpoint_v3(
        "Casey Orbit points since March 2025" + suffix,
        transport,
        start_date="2025-03-01",
        aggregation="average",
    )
    assert payload["status"] != "ok" and not payload["tables"], payload


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "suffix",
    [
        " plus 2025-03-07",
        " and between 2025-03-04 and 2025-03-06",
    ],
)
def test_explicit_total_cannot_hide_extra_date_anchors(endpoint_v3, transport, suffix):
    payload = endpoint_v3(
        "Casey Orbit total points between 2025-03-04 and 2025-03-06" + suffix,
        transport,
        aggregation="total",
    )
    assert payload["status"] != "ok" and not payload["tables"], payload


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "words,phase",
    [
        ("regular season", "Regular Season"),
        ("playoffs", "Playoffs"),
    ],
)
def test_since_month_validates_phase_and_season(endpoint_v3, transport, words, phase):
    question = "Casey Orbit points since March 2025 in 2024-25 " + words
    scope = dict(start_date="2025-03-01", aggregation="average", season_type=phase)
    good = endpoint_v3(question, transport, **scope)
    assert good["status"] == ("no_observations" if phase == "Playoffs" else "ok"), good
    if phase != "Playoffs":
        assert good["semantic_evidence"]["rows"][0]["value"] == pytest.approx(14 / 3)
    assert "aggregation_policy" not in good.get("query_plan", {})
    for mismatch in [
        {"season_type": "Regular Season" if phase == "Playoffs" else "Playoffs"},
        {"season": "2023-24"},
    ]:
        bad = endpoint_v3(question, transport, **{**scope, **mismatch})
        assert bad["status"] not in {"ok", "no_observations"} and not bad["tables"], bad


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "interval", ["between 2025-03-04 and 2025-03-06", "over 2025-03-04 to 2025-03-06"]
)
def test_alternate_interval_conflicting_year_and_fresh_request(
    endpoint_v3, transport, interval
):
    bad = endpoint_v3("Casey Orbit points " + interval + " in 2024", transport)
    assert bad["status"] != "ok" and not bad["tables"], bad
    cid = str(uuid4())
    first = endpoint_v3(
        QUESTION.replace("points", "total points"), transport, conversation=cid
    )
    assert first["status"] == "ok"
    fresh = endpoint_v3(
        "Casey Orbit points " + interval + " in 2024-25", transport, conversation=cid
    )
    assert fresh["status"] == "ok", fresh
    assert fresh["semantic_evidence"]["rows"][0]["value"] == 1.5


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("window", ["date_range", "season_to_date"])
def test_unresolved_month_and_competing_since_window(endpoint_v3, transport, window):
    for question, value in [
        ("Casey Orbit points through March 2025", "total"),
        ("Casey Orbit points since 2025", "total"),
        ("Casey Orbit points since March 2025 over the past 2 months", "average"),
    ]:
        bad = endpoint_v3(
            question,
            transport,
            window=window,
            start_date="2025-03-01" if window == "date_range" else None,
            aggregation=value,
        )
        assert bad["status"] != "ok" and not bad["tables"], bad


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_pending_dates_cannot_resume_as_guessed_total(endpoint_v3, transport):
    cid = str(uuid4())
    first = endpoint_v3(
        "Casey Orbit points since 2025-03-04", transport, conversation=cid
    )
    assert first["status"] == "clarification_required", first
    reply = endpoint_v3(
        "from 2025-03-04 through 2025-03-06", transport, conversation=cid
    )
    assert reply["status"] != "ok" and not reply["tables"], reply
    restated = endpoint_v3(QUESTION, transport, conversation=cid)
    assert restated["status"] == "ok", restated
    assert restated["semantic_evidence"]["rows"][0]["value"] == 1.5


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("open_ended", [False, True])
@pytest.mark.parametrize(
    "phase_words,phase", [("", "Regular Season"), (" playoffs", "Playoffs")]
)
def test_missing_phase_materializes_validated_scope(
    endpoint_v3, transport, open_ended, phase_words, phase
):
    question = (
        "Casey Orbit points since March 2025" if open_ended else QUESTION
    ) + phase_words
    payload = endpoint_v3(
        question,
        transport,
        omit_phase=True,
        aggregation="average",
        start_date="2025-03-01" if open_ended else "2025-03-04",
    )
    assert payload["status"] == ("no_observations" if phase == "Playoffs" else "ok"), (
        payload
    )
    assert payload["query_plan"]["queries"][0]["season_type"] == phase
    # The typed provider supplies an explicit phase; host validation checks it.
    assert payload["query_plan"]["queries"][0]["season_type"] == phase


def test_missing_both_phase_materialized_before_query():
    from tests.test_dated_points_default import QUESTION as question
    from tests.test_dated_points_default import planned

    plan = planned(
        question + " including both regular season and playoffs", season_type="Both"
    )
    from app.agent.semantic_intent import resolve_aggregation_intent

    query = dict(plan["queries"][0], aggregation="total")
    query.pop("season_type")
    raw = dict(status="query", queries=[query])
    policy = resolve_aggregation_intent(
        question + " including both regular season and playoffs",
        raw,
        {"Blair Sample": "p"},
        {"p": "Blair Sample"},
        "2024-25",
        {},
        True,
    )
    assert query["season_type"] == "Both"
    assert "season_type" in policy["derived_scope_fields"]


def test_collapsed_reference_keeps_matching_resolution():
    from app.agent.semantic_answer import normalize_reference_plan

    common = dict(metric="ast", season="2024-25", aggregation="average")
    result = normalize_reference_plan(
        dict(
            status="compare",
            queries=[
                dict(common, operation="summary", player_name="Casey Orbit"),
                dict(common, operation="rank", player_name=None),
            ],
            aggregation_resolution=[{"source": "inherited"}, {"source": "explicit"}],
        ),
        [202],
        [{"player_name": "Casey Orbit", "player_id": 202}],
    )
    assert result["aggregation_resolution"] == [{"source": "explicit"}]


def test_comparison_mixed_literal_and_relative_refuses():
    from app.agent.semantic_planner import plan_question
    from tests.test_dated_points_default import PLAYERS, client_for, raw_plan

    plan = raw_plan(aggregation="average")
    plan["status"] = "compare"
    plan["queries"].append(
        dict(plan["queries"][0], window="last_n_days", n=3, start_date=None)
    )
    result = plan_question(
        client_for(plan),
        model="test",
        question="Compare Blair Sample points between 2025-04-11 and 2025-04-13 versus last 3 days",
        selected_season="2024-25",
        players=PLAYERS,
    )
    assert result["status"] == "clarification_required"


def test_unmatched_contextual_leader_does_not_parse_reversed_interval():
    from app.agent.semantic_answer import contextual_leader_plan

    context = {
        "player_ids": [202],
        "player_names": ["Casey Orbit"],
        "scope": {
            "window": "date_range",
            "start_date": "2025-03-04",
            "as_of": "2025-03-06",
        },
    }
    assert (
        contextual_leader_plan(
            "Besides him, assists between 2025-03-06 and 2025-03-04", context, "2024-25"
        )
        is None
    )


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_since_polite_and_asof_scope_remain_supported(endpoint_v3, transport):
    polite = endpoint_v3(
        "May I see Casey Orbit points since March 2025",
        transport,
        start_date="2025-03-01",
        aggregation="average",
    )
    assert polite["status"] == "ok", polite
    anchor = endpoint_v3(
        "Casey Orbit points in 2024-25 as of 2025-03-06",
        transport,
        window="season_to_date",
        start_date=None,
    )
    assert anchor["status"] == "ok", anchor


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "phase,words",
    [
        ("Both", " including both regular season and playoffs"),
        ("Playoffs", " playoffs"),
    ],
)
def test_missing_phase_preserves_context_and_fresh_request_resets(
    endpoint_v3, transport, phase, words
):
    cid = str(uuid4())
    dates = dict(start_date="2025-03-09", as_of="2025-03-10")
    question = "Casey Orbit points between 2025-03-09 and 2025-03-10 in 2024-25" + words
    first = endpoint_v3(
        question, transport, conversation=cid, season_type=phase, **dates
    )
    assert first["status"] == "ok", first
    followup = endpoint_v3(
        "What about points?",
        transport,
        conversation=cid,
        omit_phase=True,
        aggregation="average",
        **dates,
    )
    assert followup["status"] == "ok", followup
    assert followup["query_plan"]["queries"][0]["season_type"] == phase
    bad = endpoint_v3(
        "What about points?",
        transport,
        conversation=cid,
        season_type="Regular Season",
        aggregation="average",
        **dates,
    )
    assert bad["status"] != "ok" and not bad["tables"], bad
    fresh = endpoint_v3(QUESTION, transport, conversation=cid, omit_phase=True)
    assert fresh["status"] == "ok", fresh
    assert fresh["query_plan"]["queries"][0]["season_type"] == "Regular Season"


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("value,expected", [("total", 3), ("average", 1.5)])
def test_explicit_clarification_reply_remains_supported(
    endpoint_v3, transport, value, expected
):
    cid = str(uuid4())
    first = endpoint_v3(
        "Casey Orbit points since 2025-03-04", transport, conversation=cid
    )
    assert first["status"] == "clarification_required"
    reply = endpoint_v3(
        value + " points from 2025-03-04 through 2025-03-06",
        transport,
        conversation=cid,
        aggregation=value,
    )
    assert reply["status"] == "ok", reply
    assert reply["semantic_evidence"]["rows"][0]["value"] == expected


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "words,phase",
    [
        ("regular season", "Regular Season"),
        ("playoffs", "Playoffs"),
        ("including both regular season and playoffs", "Both"),
    ],
)
def test_partial_explicit_phase_overrides_prior_scope(
    endpoint_v3, transport, words, phase
):
    cid = str(uuid4())
    first = endpoint_v3(QUESTION, transport, conversation=cid)
    assert first["status"] == "ok"
    request = "What about points " + words + "?"
    result = endpoint_v3(
        request, transport, conversation=cid, omit_phase=True, aggregation="average"
    )
    assert result["status"] == ("no_observations" if phase == "Playoffs" else "ok"), (
        result
    )
    assert result["query_plan"]["queries"][0]["season_type"] == phase
    bad = endpoint_v3(
        request,
        transport,
        conversation=cid,
        season_type="Playoffs" if phase != "Playoffs" else "Regular Season",
        aggregation="average",
    )
    assert bad["status"] not in {"ok", "no_observations"} and not bad["tables"], bad


# Public orchestration tests use authored plans through the provider contract.
pytestmark = pytest.mark.usefixtures("scripted_intelligence")
