"""Independent regressions for the sole external G review findings."""

import copy
import json
from dataclasses import asdict
from datetime import date
from decimal import Decimal

import pytest
from app.agent.claims import expand_claim, finalize, validate
from app.agent.semantic_answer import add_leaderboard_reference, render_answer
from app.agent.semantic_serving import source_players
from app.agent.semantics import Query, run_query
from scripts.ask_eval.fixtures import FrozenWarehouse
from tests.test_ask_harness import SOURCE, agent
from tests.test_claim_bindings import literal_breakdown
from tests.test_dated_points_default import endpoint  # noqa: F401


def source():
    return FrozenWarehouse(copy.deepcopy(SOURCE), "complete").load(["2024-25"])[1]


def query_payload(query, players=None):
    e = source()
    players = source_players(source()) if players is None else players
    result = {
        "status": "ok",
        "resolved_queries": [asdict(query)],
        "evidence": run_query(e, query),
    }
    return render_answer(result, players), result, e


@pytest.mark.parametrize("family", ["overview", "comparison", "explorer"])
def test_report_exact_counts_bounded_samples_for_large_visible_families(family):
    points = [{"x": i, "y": i + 0.5, "label": "example"} for i in range(10000)]
    payload = (
        {
            "semantic_evidence": {
                "metrics" if family == "overview" else "comparison_sections": points
            }
        }
        if family != "explorer"
        else {"comparison_explorer": points}
    )
    report = validate(payload)
    assert report["unbound_count"] == 30000
    assert sum(report["unbound_counts"].values()) == 30000
    assert len(report["unbound"]) == 32 and report["unbound_truncated"]
    assert report["status"] == "incomplete"
    assert len(json.dumps(report)) < 4000


def test_unbound_legacy_shape_never_runs_bound_route_guards():
    assert (
        finalize({"semantic_evidence": {"metric": {}}, "answer": "Legacy"})["answer"]
        == "Legacy"
    )
    payload = literal_breakdown()
    payload["semantic_evidence"] = {"metric": {}}
    assert finalize(payload, mode="enforce")["status"] == "error"


def test_supported_date_decimal_boundary_and_nonfinite_safe_error():
    result = literal_breakdown()["research"]
    result["scope"]["start"] = date(2024, 11, 1)
    for player in result["players"]:
        player["metrics"][0]["result"]["value"] = Decimal("5.0")
        player["metrics"][0]["result"]["display_value"] = Decimal("5.0")
    from app.research import answer_payload

    payload = finalize(answer_payload(result))
    assert (
        payload.get("status") != "error" and payload["tables"][0]["rows"][0][2] == 5.0
    )
    assert payload["research"]["scope"]["start"] == "2024-11-01"
    result["players"][0]["metrics"][0]["result"]["value"] = float("nan")
    bad = answer_payload(result)
    bad.update(
        conversation_id="same-conversation",
        ask_route={"route": "research"},
        query_plan={"route": "research"},
    )
    safe = finalize(bad)
    assert safe["status"] == "error" and safe["conversation_id"] == "same-conversation"
    assert (
        safe["ask_route"]["route"] == "research"
        and safe["query_plan"]["route"] == "research"
    )
    json.dumps(safe, allow_nan=False)


@pytest.mark.parametrize("named", [True, False])
def test_multisection_game_logs_selection_keeps_exact_source_provenance(named):
    e = source()
    queries = [
        Query("pts", "2024-25", "total", player_id=pid, operation="game_log", limit=100)
        for pid in (101, 202)
    ]
    results = [run_query(e, q) for q in queries]
    payload = render_answer(
        {
            "status": "ok",
            "resolved_queries": [asdict(q) for q in queries],
            "evidence": {
                "current": results[0],
                "baseline": results[1],
                "difference": None,
                "overlapping_game_ids": [],
            },
        },
        source_players(source()) if named else [],
    )
    assert len(payload["charts"]) == 2
    assert validate(payload)["errors"] == []
    second = payload["charts"][1]
    from app.agent.visualization import VisualizationAgent
    from tests.test_visualization import Selector

    payload = VisualizationAgent().enrich(
        Selector("1"), payload, "Show the second player game log", "openai", "test"
    )
    assert payload["visualization"]["selection"] == "model"
    assert payload["charts"][0]["series"] == second["series"]
    assert validate(payload)["errors"] == []
    manifest = payload["claim_bindings"]
    c = next(
        expand_claim(manifest, c)
        for c in manifest["claims"]
        if expand_claim(manifest, c)["destination"]
        == ["charts", 0, "series", 0, "points", 0, "y"]
    )
    assert c["sources"] == [["semantic_evidence", "baseline", "rows", 0, "value"]]
    manifest["chart_selection"] = [0]
    assert validate(payload)["status"] == "invalid"


def test_ratio_reference_outside_rank_set_uses_independent_identity_and_scale():
    q = Query("fg_pct", "2024-25", "ratio", operation="rank", limit=1, min_attempts=1)
    payload, result, evidence = query_payload(q)
    add_leaderboard_reference(payload, result, evidence, source_players(source()), 101)
    assert validate(payload)["errors"] == []
    reference = payload["reference_evidence"]["rows"][0]
    assert reference["value"] == pytest.approx(3 / 30)
    assert payload["tables"][0]["rows"][-1][1] == "10.0"
    assert payload["reference_evidence"]["identity"]["player_id"] == 101
    payload["tables"][0]["rows"][-1][1] = "0.1"
    assert validate(payload)["status"] == "invalid"


@pytest.mark.parametrize(
    "question",
    [
        "Who is similar to Avery Example in 2024-25?",
        "Who is similar to Avery Example in the last 5 games in 2024-25?",
        "Compare Avery Example points last 2 games against league average in 2024-25",
    ],
)
def test_reference_adapter_controls_and_changed_cells(question):
    payload = agent().answer(question)
    assert payload["status"] == "ok" and validate(payload)["errors"] == []
    manifest = payload["claim_bindings"]
    destination = next(
        expand_claim(manifest, c)["destination"]
        for c in manifest["claims"]
        if expand_claim(manifest, c)["destination"][0] == "tables"
    )
    payload["tables"][destination[1]]["rows"][destination[3]][destination[4]] = (
        "contradicted"
    )
    assert validate(payload)["status"] == "invalid"


def test_frozen_study_nine_fields_seven_cells_and_focal_scope(tmp_path):
    from tests.test_teammate_ask import setup

    a, _ = setup(tmp_path)
    payload = a.answer("Using the teammate study, is the difference significant?")
    assert payload["study_status"] == "answered" and validate(payload)["errors"] == []
    manifest = payload["claim_bindings"]
    claims = [expand_claim(manifest, c) for c in manifest["claims"]]
    assert len(claims) == 7
    assert (
        sum(len(c["sources"]) for c in claims) == 10
    )  # 9 fields; two interval endpoints.
    assert payload["claim_evidence"]["frozen_study"]["scope"]["focal_player_id"] == 1
    payload["tables"][0]["rows"][2][1] = "+9.88"
    assert validate(payload)["status"] == "invalid"


def test_availability_ratio_is_already_percentage_points_and_contracts_cached(
    tmp_path, monkeypatch
):
    from app.agent import claim_contexts
    from app.agent.availability_ask import render_answer as render_availability
    from app.availability import compare_availability, load_availability
    from tests.test_availability import bundle, request

    path, _ = bundle.__wrapped__(tmp_path)
    loaded = load_availability(str(path))
    result = compare_availability(
        loaded,
        request(
            loaded, "Show Avery Finch field goal percentage when Blake Reed was out"
        ),
    )
    assert result["metrics"][0]["difference"] == 40  # 5/10 - 1/10 => 40 pp.
    # Audit scale regression uses80 canonical records without timing assertions.
    row = result["groups"]["both_played"][0]
    result["groups"]["both_played"] = [
        dict(copy.deepcopy(row), game_id=f"audit-{i:03}", report_status=None)
        for i in range(80)
    ]
    payload = render_availability(result, source_players(loaded[1]))
    from app.agent.claim_renderers import availability_table

    availability_table(payload)
    assert payload["tables"][0]["rows"][0][1:4] == ["10.0", "50.0", "40.0"]
    original, calls = claim_contexts.contracts, []

    def counted(p):
        calls.append(1)
        return original(p)

    monkeypatch.setattr(claim_contexts, "contracts", counted)
    assert validate(payload)["errors"] == []
    assert len(calls) == 1
    payload["tables"][0]["rows"][0][3] = "0.4"
    assert validate(payload)["status"] == "invalid"


def test_split_ratio_uses_percentage_points_without_double_scaling():
    from tests.test_player_splits import answer
    from tests.test_player_splits import source as split_source

    payload = answer(
        split_source.__wrapped__(),
        "Compare Avery Example FG% home vs away in 2024-25 regular season against BBB",
    )
    assert payload["tables"][0]["rows"][0][1:4] == ["0.00%", "100.00%", "-100.00 pp"]
    assert validate(payload)["errors"] == []


def test_percent_research_and_published_default_unit_controls():
    from app.research import answer_payload

    payload = literal_breakdown()
    result = payload["research"]
    for player in result["players"]:
        metric = player["metrics"][0]
        metric.update(metric="fg_pct", label="FG%", unit="percent", aggregation="ratio")
        metric["result"].update(value=0.25, display_value=25.0)
    payload = answer_payload(result)
    assert payload["tables"][0]["rows"][0][2] == 25.0
    assert validate(payload)["errors"] == []
    from app.research_studies import PAIRS, pending, study_answer

    study = pending(PAIRS[0])
    study["scope"] = {
        "season": "2024-25",
        "phase": "Regular Season",
        "start": "2024-11-01",
        "end": "2024-11-02",
    }
    study["metrics"] = [
        dict(
            study["metrics"][0],
            label="PTS",
            descriptive={"participated": 1.0, "reported_out": 2.0, "difference": 1.0},
        )
    ]
    published = study_answer(study, ["pts"])
    assert published["tables"] and validate(published)["errors"] == []
    # Adapter-only check uses an independently declared expected default, since
    # the publication analysis requires additional fields outside this envelope.
    from app.agent.claim_renderers import published_study_table

    control = {
        "tables": [
            {"rows": [[study["metrics"][0]["label"], "1.0", "2.0", "+1.0", "per game"]]}
        ]
    }
    study["metrics"] = [study["metrics"][0]]
    study["metrics"][0]["descriptive"] = {
        "participated": 1.0,
        "reported_out": 2.0,
        "difference": 1.0,
    }
    published_study_table(control, study, {study["metrics"][0]["metric"]})
    assert validate(control)["errors"] == []


def test_award_composition_and_zero_appearance_control():
    from types import SimpleNamespace

    from app.agent.service import StatsAgent
    from tests.test_award_lookup import warehouse
    from tests.test_semantic_migration import settings

    a = StatsAgent(
        settings("2024-25"), SimpleNamespace(), semantic_warehouse=warehouse()
    )
    payload = a.answer("Who won Rookie of the Year in 2024-25? How did he play?")
    assert payload["status"] == "ok" and validate(payload)["errors"] == []
    from app.agent.appearance_ask import answer_appearances

    zero = answer_appearances(
        agent(),
        "How many games did Avery Example play from 2025-04-01 through 2025-04-02 in 2024-25?",
        {},
        None,
    )
    assert not zero["tables"]
    assert "no zero was assumed" in zero["answer"]
    assert validate(zero)["errors"] == []


def test_mismatch_rolls_back_pending_clear_and_omits_unsafe_plan(monkeypatch):
    from app.agent.conversation import InMemoryConversationStore
    from app.agent.execution import governed_request

    store = InMemoryConversationStore()
    store.set_pending_clarification("c", question="Pending", query_plan={})

    @governed_request
    def request(self):
        store.clear_pending_clarification("c")
        p = literal_breakdown()
        p["tables"][0]["rows"][0][2] = -1
        p.update(conversation_id="c", query_plan={"bad": float("nan")})
        return finalize(p)

    safe = request(agent())
    assert store.get_pending_clarification("c") is not None
    assert safe["conversation_id"] == "c" and "query_plan" not in safe
    json.dumps(safe, allow_nan=False)


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_bind_time_nonfinite_source_returns_governed_endpoint_error(
    request, monkeypatch, transport
):
    from app.agent.visualization import VisualizationAgent
    from app.research import answer_payload

    def invalid_source(self, *args, **kwargs):
        result = literal_breakdown()["research"]
        result["players"][0]["metrics"][0]["result"]["value"] = float("nan")
        return answer_payload(result)

    monkeypatch.setattr(VisualizationAgent, "enrich", invalid_source)
    payload = request.getfixturevalue("endpoint")(transport=transport)
    assert (
        payload["status"] == "error"
        and payload["reason_code"] == "claim_binding_mismatch"
    )
    json.dumps(payload, allow_nan=False)
