"""Independent hand-derived values and adversarial evidence/presentation changes."""

import copy
import json
from types import SimpleNamespace

import pytest
from app.agent.claims import finalize, transform, validate
from app.agent.service import StatsAgent
from app.research import answer_payload
from tests.test_semantic_migration import Model, Warehouse, settings


def literal_breakdown():
    # Independently calculated: (0 + 10)/2 = 5; equal values across two entities.
    result = {
        "scope": {
            "season": "2024-25",
            "phase": "Regular Season",
            "start": "2024-11-01",
            "end": "2024-11-02",
        },
        "limitations": [],
        "players": [
            {
                "player_id": pid,
                "player_name": name,
                "filter_missing_games": 0,
                "metrics": [
                    {
                        "metric": "pts",
                        "label": "PTS",
                        "unit": "count",
                        "aggregation": "average",
                        "result": {
                            "value": 5.0,
                            "display_value": 5.0,
                            "valid_games": 2,
                            "observed_games": 2,
                        },
                    }
                ],
            }
            for pid, name in ((41, "Robin Fiction"), (82, "Sky Example"))
        ],
    }
    return answer_payload(result)


def value_claim(payload):
    return next(
        c
        for c in payload["claim_bindings"]["claims"]
        if c["destination"] == ["tables", 0, "rows", 0, 2]
    )


def test_literal_control_and_missing_mapping_are_incomplete_never_universal():
    payload = literal_breakdown()
    assert payload["tables"][0]["rows"] == [
        ["Robin Fiction", "PTS", 5.0, "count", "average", 2, 2, 0],
        ["Sky Example", "PTS", 5.0, "count", "average", 2, 2, 0],
    ]
    report = validate(payload)
    assert report["errors"] == [] and report["status"] == "incomplete"
    assert report["bound_destinations"] == 16
    payload["claim_bindings"]["claims"].remove(value_claim(payload))
    report = validate(payload)
    assert report["status"] == "incomplete"
    assert ["tables", 0, "rows", 0, 2] in report["unbound"]


@pytest.mark.parametrize(
    "mutation",
    [
        "value",
        "same_value_identity_swap",
        "same_value_source_swap",
        "metric",
        "unit",
        "scope",
        "phase",
        "source",
        "hash",
        "missing_hash",
        "transform",
        "duplicate",
        "unsafe_path",
        "wrong_type",
        "label",
    ],
)
def test_independent_mutations_fail_at_a_specific_destination(mutation):
    p = literal_breakdown()
    c = value_claim(p)
    if mutation == "value":
        p["tables"][0]["rows"][0][2] = -5.0
    elif mutation == "same_value_identity_swap":
        p["tables"][0]["rows"].reverse()
    elif mutation == "same_value_source_swap":
        c["sources"][0][2] = 1
    elif mutation in ("metric", "unit"):
        c[mutation] = "ast" if mutation == "metric" else "percent"
    elif mutation in ("scope", "phase"):
        p["research"]["scope"]["end" if mutation == "scope" else "phase"] = (
            "2024-11-03" if mutation == "scope" else "Playoffs"
        )
    elif mutation == "source":
        p["research"]["players"][0]["metrics"][0]["result"]["value"] = 99
    elif mutation == "hash":
        c["evidence_sha256"]["research"] = "0" * 64
    elif mutation == "missing_hash":
        c["evidence_sha256"].clear()
    elif mutation == "transform":
        c["transform"] = {"op": "template", "template": "5.0"}
    elif mutation == "duplicate":
        p["claim_bindings"]["claims"].append(copy.deepcopy(c))
    elif mutation == "unsafe_path":
        c["sources"][0] = ["research", "__class__"]
    elif mutation == "wrong_type":
        p["tables"][0]["rows"][0][7] = False
    elif mutation == "label":
        p["tables"][0]["rows"][0][1] = "AST"
    report = validate(p)
    assert report["status"] == "invalid"
    assert report["errors"] and all(e["destination"] for e in report["errors"])
    safe = finalize(p)
    assert safe["status"] == "error" and not safe["tables"]


@pytest.mark.parametrize(
    "template,values",
    [("{0}", [1, 2]), ("5.0", [5]), ("{0.real}", [1]), ("{0[0]}", [[1]])],
)
def test_templates_cannot_hide_numeric_literals_or_omit_operands(template, values):
    with pytest.raises((ValueError, KeyError)):
        transform(values, {"op": "template", "template": template})


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_actual_endpoint_withholds_contradicted_bound_claim_before_stream(
    monkeypatch, transport
):
    from app.agent.visualization import VisualizationAgent
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository
    from fastapi.testclient import TestClient

    repo = BigQueryWarehouseRepository(settings(), client=SimpleNamespace())
    repo._governed_warehouse = Warehouse()
    original = VisualizationAgent.enrich

    def corrupt(self, *args, **kwargs):
        payload = original(self, *args, **kwargs)
        payload["tables"][0]["rows"][0][1] = "99999.0%"
        return payload

    monkeypatch.setattr(VisualizationAgent, "enrich", corrupt)
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
                "/api/agent/ask"
                + ("/stream" if transport == "sse" else "")
                + "?season=2024-25",
                json={"question": "Small Sample FG% this season"},
            )
        assert response.status_code == 200
        assert "99999" not in response.text
        if transport == "json":
            payload = response.json()
        else:
            events = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            payload = next(e["payload"] for e in events if e.get("type") == "final")
            assert not any(
                "10.0%" in str(e) for e in events if e.get("type") == "answer_delta"
            )
        assert payload["reason_code"] == "claim_binding_mismatch"
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)


def test_game_log_control_and_exact_chart_mutations():
    p = StatsAgent(
        settings(),
        SimpleNamespace(),
        client=Model("Fantasy Score", operation="game_log", window="last_n_games", n=2),
        semantic_warehouse=Warehouse(),
    ).answer("Regular Player Fantasy Score game log for the last 2 appearances")
    assert validate(p)["errors"] == []
    p["charts"][0]["series"][0]["points"][0]["y"] += 1
    assert any(
        e["destination"] == ["charts", 0, "series", 0, "points", 0, "y"]
        for e in validate(p)["errors"]
    )


def test_recorded_zero_null_and_zero_attempts_remain_distinct():
    from app.agent.semantics import aggregate, load_contract

    contract = load_contract()
    rows = [
        dict(
            season="2024-25", game_id="zero", player_id=41, min=20, pts=0, fga=0, fgm=0
        ),
        dict(
            season="2024-25", game_id="ten", player_id=41, min=30, pts=10, fga=8, fgm=4
        ),
    ]
    pts = aggregate(rows, contract.metric("pts"), "average")
    attempts = aggregate(rows, contract.metric("fga"), "average")
    assert (
        pts["value"],
        pts["valid_games"],
        pts["observed_games"],
        pts["denominator"],
    ) == (5, 2, 2, 2)
    assert attempts["value"] == 4
    assert aggregate(rows, contract.metric("gp"), "total")["value"] == 2
    missing = copy.deepcopy(rows)
    missing[0]["pts"] = None
    partial = aggregate(missing, contract.metric("pts"), "average")
    assert (
        partial["value"],
        partial["valid_games"],
        partial["observed_games"],
        partial["missing_component_games"],
    ) == (10, 1, 2, 1)
    assert aggregate(missing, contract.metric("gp"), "total")["value"] == 2
    assert aggregate(rows[:1], contract.metric("fg_pct"), "ratio")["value"] is None
    # Zero attempts never imply zero scoring: free throws can produce points.
    free_throws = [{**rows[0], "pts": 3}]
    assert aggregate(free_throws, contract.metric("pts"), "average")["value"] == 3


def test_hundred_rows_multimetric_hashes_evidence_once_per_stage(monkeypatch):
    from app.agent import claims

    result = literal_breakdown()["research"]
    result["players"] = [
        dict(
            copy.deepcopy(result["players"][0]),
            player_id=1000 + i,
            player_name=f"Synthetic Player {i}",
        )
        for i in range(100)
    ]
    for player in result["players"]:
        second = copy.deepcopy(player["metrics"][0])
        second.update(metric="ast", label="AST")
        player["metrics"].append(second)
    original, root_calls = claims.digest, []

    def counted(value):
        if value is result:
            root_calls.append(1)
        return original(value)

    monkeypatch.setattr(claims, "digest", counted)
    p = answer_payload(result)
    assert len(root_calls) == 1
    assert len(p["claim_bindings"]["claims"]) == 1600
    assert validate(p)["errors"] == []
    assert len(root_calls) == 2  # One additional independent final validation.


def test_scope_and_pooled_denominator_prose_has_exact_destinations():
    p = StatsAgent(
        settings(),
        SimpleNamespace(),
        client=Model("fg_pct"),
        semantic_warehouse=Warehouse(),
    ).answer("Small Sample FG% this season")
    assert validate(p)["errors"] == []
    numeric = next(
        (i, text) for i, text in enumerate(p["assumptions"]) if "numerator" in text
    )
    assert "numerator 1.0; denominator 10.0" in numeric[1]
    p["assumptions"][numeric[0]] = numeric[1].replace("10.0", "2.0")
    assert any(
        e["destination"] == ["assumptions", numeric[0]] for e in validate(p)["errors"]
    )


def test_contradicted_claim_does_not_commit_successful_conversation(monkeypatch):
    from app.agent.conversation import InMemoryConversationStore
    from app.agent.visualization import VisualizationAgent

    store = InMemoryConversationStore()
    agent = StatsAgent(
        settings(),
        SimpleNamespace(),
        client=Model("fg_pct"),
        semantic_warehouse=Warehouse(),
        conversation_store=store,
    )
    original = VisualizationAgent.enrich

    def corrupt(self, *args, **kwargs):
        payload = original(self, *args, **kwargs)
        payload["tables"][0]["rows"][0][1] = "9000.0%"
        return payload

    monkeypatch.setattr(VisualizationAgent, "enrich", corrupt)
    response = agent.answer(
        "Small Sample FG% this season", conversation_id="invalid-binding"
    )
    assert response["status"] == "error"
    assert not store.get_turns("invalid-binding", max_turns=10)


def test_verified_dnp_unknown_and_metric_samples_stay_distinct(tmp_path):
    from app.agent.availability_ask import render_answer
    from app.agent.claim_renderers import availability_table
    from app.agent.semantic_serving import source_players
    from app.availability import compare_availability, load_availability
    from tests.test_availability import bundle, request

    path, _ = bundle.__wrapped__(tmp_path)
    loaded = load_availability(str(path))
    # Verified Out/no appearance is a supported nonparticipation group; an absent
    # record without verification is unknown, not an invented DNP or zero game.
    for row in loaded[1].rows:
        if row["player_id"] == 811 and row["game_id"] == "g2":
            row["pts"] = None
    result = compare_availability(loaded, request(loaded))
    assert [r["game_id"] for r in result["groups"]["did_not_play"]] == ["g2", "g4"]
    assert result["excluded"]["unverified_status_or_membership"] == 1
    assert "g5" not in [
        r["game_id"] for group in result["groups"].values() for r in group
    ]
    payload = render_answer(result, source_players(loaded[1]))
    availability_table(payload)
    assert payload["tables"][0]["rows"][0][-1] == "1 of 2"
    assert validate(payload)["errors"] == []
    payload["tables"][0]["rows"][0][-1] = "2 of 2"
    assert any(
        e["destination"] == ["tables", 0, "rows", 0, 6]
        for e in validate(payload)["errors"]
    )
