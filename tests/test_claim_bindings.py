"""Independent hand-derived values and adversarial evidence/presentation changes."""

import copy
import io
import json
import logging
from types import SimpleNamespace

import pytest
from app.agent.claims import expand_path, finalize, transform, validate
from app.agent.service import StatsAgent
from app.research import answer_payload
from tests.test_semantic_migration import Model, Warehouse, settings


@pytest.fixture
def observation_stream():
    from app.agent.claim_rollout import logger

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)


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
        if expand_path(payload["claim_bindings"], c["d"]) == ["tables", 0, "rows", 0, 2]
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
        c["s"] = next(
            other["s"]
            for other in p["claim_bindings"]["claims"]
            if expand_path(p["claim_bindings"], other["d"])
            == ["tables", 0, "rows", 1, 2]
        )
    elif mutation in ("metric", "unit"):
        p["claim_bindings"]["descriptors"][c["m"]][0 if mutation == "metric" else 1] = (
            "ast" if mutation == "metric" else "percent"
        )
    elif mutation in ("scope", "phase"):
        p["research"]["scope"]["end" if mutation == "scope" else "phase"] = (
            "2024-11-03" if mutation == "scope" else "Playoffs"
        )
    elif mutation == "source":
        p["research"]["players"][0]["metrics"][0]["result"]["value"] = 99
    elif mutation == "hash":
        p["claim_bindings"]["evidence_sha256"]["research"] = "0" * 64
    elif mutation == "missing_hash":
        p["claim_bindings"]["evidence_sha256"].clear()
    elif mutation == "transform":
        p["claim_bindings"]["transforms"][c["t"]] = {
            "op": "template",
            "template": "5.0",
        }
    elif mutation == "duplicate":
        p["claim_bindings"]["claims"].append(copy.deepcopy(c))
    elif mutation == "unsafe_path":
        p["claim_bindings"]["paths"][c["s"][0]][1] = "__class__"
    elif mutation == "wrong_type":
        p["tables"][0]["rows"][0][7] = False
    elif mutation == "label":
        p["tables"][0]["rows"][0][1] = "AST"
    report = validate(p)
    assert report["status"] == "invalid"
    assert report["errors"]
    if mutation not in {"scope", "phase", "source", "hash", "unsafe_path"}:
        assert any(e["destination"] for e in report["errors"])
    safe = finalize(p, mode="enforce")
    assert safe["status"] == "error" and not safe["tables"]


@pytest.mark.parametrize(
    "template,values",
    [("{0}", [1, 2]), ("5.0", [5]), ("{0.real}", [1]), ("{0[0]}", [[1]])],
)
def test_templates_cannot_hide_numeric_literals_or_omit_operands(template, values):
    with pytest.raises((ValueError, KeyError)):
        transform(values, {"op": "template", "template": template})


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("mode", ["log", "enforce"])
@pytest.mark.parametrize(
    "scenario",
    ["mismatch", "incomplete", "refusal", "bytes", "set", "object", "recursive"],
)
def test_actual_endpoint_withholds_contradicted_bound_claim_before_stream(
    monkeypatch, transport, mode, scenario, observation_stream
):
    from dataclasses import replace

    from app.agent.visualization import VisualizationAgent
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository
    from fastapi.testclient import TestClient

    repo = BigQueryWarehouseRepository(settings(), client=SimpleNamespace())
    repo._governed_warehouse = Warehouse()
    original = VisualizationAgent.enrich

    def corrupt(self, *args, **kwargs):
        payload = original(self, *args, **kwargs)
        if scenario == "mismatch":
            payload["tables"][0]["rows"][0][1] = "99999.0%"
        elif scenario in {"bytes", "set", "object", "recursive"}:
            payload["unsafe"] = {
                "bytes": b"private",
                "set": {1},
                "object": object(),
                "recursive": payload,
            }[scenario]
        return payload

    monkeypatch.setattr(VisualizationAgent, "enrich", corrupt)
    old = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: lambda: replace(settings(), ask_claim_enforcement=mode),
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
                json={
                    "question": "How did Small Sample play in the conference finals?"
                    if scenario == "refusal"
                    else "Small Sample FG% this season"
                },
            )
        assert response.status_code == 200
        assert ("99999" in response.text) == (mode == "log" and scenario == "mismatch")
        if transport == "json":
            payload = response.json()
        else:
            events = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            payload = next(e["payload"] for e in events if e.get("type") == "final")
            if mode == "enforce" and scenario == "mismatch":
                assert not any(
                    "10.0%" in str(e) for e in events if e.get("type") == "answer_delta"
                )
        unsafe = scenario in {"bytes", "set", "object", "recursive"}
        expected_status = (
            "invalid" if scenario == "mismatch" or unsafe else "incomplete"
        )
        assert payload["claim_validation"]["status"] == expected_status
        assert payload["claim_validation"]["mode"] == mode
        emitted = [
            json.loads(line.removeprefix("ask_claim_observation "))
            for line in observation_stream.getvalue().splitlines()
        ]
        assert len(emitted) == 1
        assert emitted[0]["route"] == (
            "unsupported" if scenario == "refusal" else "governed_metrics"
        )
        assert emitted[0]["mode"] == mode
        assert emitted[0]["assessed"] == 1
        assert emitted[0]["status"] == expected_status
        if unsafe:
            assert payload["status"] == "error"
            assert emitted[0]["reason"] == "serialization"
            assert "_claim_binding_error" not in payload
            json.dumps(payload, allow_nan=False)
        elif scenario == "refusal":
            assert payload["status"] != "ok" and not payload["tables"]
            assert payload["ask_route"]["reason_code"] == "unsupported_round"
        elif scenario == "mismatch" and mode == "enforce":
            assert payload["reason_code"] == "claim_binding_mismatch"
        else:
            assert payload["status"] == "ok"
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
        if isinstance(value, dict) and len(value.get("players", [])) == 100:
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


@pytest.mark.parametrize("mode", ["log", "enforce"])
def test_contradicted_claim_does_not_commit_successful_conversation(monkeypatch, mode):
    from dataclasses import replace

    from app.agent.conversation import InMemoryConversationStore
    from app.agent.visualization import VisualizationAgent

    store = InMemoryConversationStore()
    agent = StatsAgent(
        replace(settings(), ask_claim_enforcement=mode),
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
    assert response["status"] == ("error" if mode == "enforce" else "ok")
    assert bool(store.get_turns("invalid-binding", max_turns=10)) == (mode == "log")


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


@pytest.mark.parametrize("surface", ["metric", "season", "phase", "column_unit"])
def test_unchanged_numeric_values_do_not_validate_changed_metric_scope_labels(surface):
    p = StatsAgent(
        settings(),
        SimpleNamespace(),
        client=Model("fg_pct"),
        semantic_warehouse=Warehouse(),
    ).answer("Small Sample FG% this season")
    assert validate(p)["errors"] == []
    before = copy.deepcopy(p["tables"][0]["rows"])
    if surface == "column_unit":
        p["tables"][0]["columns"][1]["label"] = "Assists per game"
        destination = ["tables", 0, "columns", 1, "label"]
    else:
        old, new = {
            "metric": ("FG%", "AST"),
            "season": ("2024-25", "2023-24"),
            "phase": ("Regular Season", "Playoffs"),
        }[surface]
        assert old in p["tables"][0]["title"]
        p["tables"][0]["title"] = p["tables"][0]["title"].replace(old, new)
        destination = ["tables", 0, "title"]
    assert p["tables"][0]["rows"] == before
    assert any(error["destination"] == destination for error in validate(p)["errors"])


def test_split_sides_scope_and_sample_labels_are_independently_bound():
    from app.agent.player_splits import answer_split
    from app.agent.semantic_serving import source_players
    from tests.test_player_splits import source

    evidence = source.__wrapped__()
    p = answer_split(
        "Compare Avery Example points home vs away 2024-25",
        evidence,
        source_players(evidence),
        "2024-25",
    )
    assert validate(p)["errors"] == []
    assert [c["label"] for c in p["tables"][0]["columns"]][1:3] == ["Away", "Home"]
    for path, wrong in [
        (["tables", 0, "columns", 1, "label"], "Home"),
        (["tables", 0, "rows", 0, 0], "AST per game"),
        (["tables", 0, "rows", 0, 4], "999 of 999"),
        (["assumptions", 0], "2023-24; wrong scope"),
    ]:
        changed = copy.deepcopy(p)
        target = changed
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = wrong
        assert any(
            error["destination"] == path for error in validate(changed)["errors"]
        )


def test_appearance_prose_dates_names_and_counts_bind_without_numeric_search():
    from tests.test_ask_harness import agent

    p = agent().answer(
        "How many games did Avery Example and Blair Sample play in 2024-25?"
    )
    assert validate(p)["errors"] == []
    assert "Avery Example: 6 games played" in p["answer"]
    for old, new in [
        ("2024-25", "2023-24"),
        ("regular season", "playoffs"),
        ("Avery Example", "Blair Sample"),
        ("6 games", "7 games"),
    ]:
        changed = copy.deepcopy(p)
        changed["answer"] = changed["answer"].replace(old, new)
        assert any(
            error["destination"] == ["answer"] for error in validate(changed)["errors"]
        )


def maximum_rank_payload(*, single_query=False):
    from dataclasses import asdict

    from app.agent.semantic_answer import render_answer
    from app.agent.semantics import COMPONENTS, Evidence, Query, run_query

    players = [
        {"player_id": i, "player_name": f"Synthetic Player {i}"} for i in range(1, 101)
    ]
    rows = [
        {
            **dict.fromkeys(COMPONENTS, 0),
            **p,
            "season": "2024-25",
            "season_type": "Regular Season",
            "game_id": f"g{day}",
            "game_date": f"2024-11-0{day}",
            "team_abbr": "AAA",
            "opponent_abbr": "BBB",
            "home_away": "home",
            "min": 30,
            "pts": p["player_id"],
            "ast": p["player_id"] / 10,
        }
        for p in players
        for day in range(1, 6)
    ]
    evidence = Evidence(
        rows,
        frozenset({("2024-25", "Regular Season")}),
        "synthetic",
        "max-ranks",
        {("2024-25", "Regular Season"): "2024-11-05"},
        complete=True,
    )
    if single_query:
        from app.agent.semantic_planner import execute_plan

        executed = execute_plan(
            {
                "status": "query",
                "message": "",
                "queries": [
                    {
                        "metric": "pts",
                        "season": "2024-25",
                        "aggregation": "average",
                        "operation": "rank",
                        "limit": 100,
                        "player_name": None,
                    }
                ],
            },
            evidence,
            players,
        )
        assert executed["status"] == "ok"
        return finalize(render_answer(executed, players))
    # Conservative renderer stress only: planner comparison rejects two ranks.
    queries = [
        Query(metric, "2024-25", "average", operation="rank", limit=100)
        for metric in ("pts", "ast")
    ]
    results = [run_query(evidence, q) for q in queries]
    return finalize(
        render_answer(
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
            players,
        )
    )


def test_maximum_rank_payload_size_terminal_delivery_and_local_history(tmp_path):
    import asyncio

    from app.agent.capacity import Delivery
    from app.agent.history import append_history_turn, read_history

    p = maximum_rank_payload()
    assert p["status"] == "ok" and not p["claim_validation"]["errors"]
    assert [len(table["rows"]) for table in p["tables"]] == [100, 100]
    serialized = json.dumps(p, ensure_ascii=False).encode()
    metadata = json.dumps(p["claim_bindings"], ensure_ascii=False).encode()
    # A regression budget, not a new production truncation or request limit.
    assert len(serialized) < 500_000 and len(metadata) < 400_000

    async def deliver():
        delivery = Delivery(max_bytes=1650, max_events=24)
        delivery.finish(serialized)
        assert await delivery.next() == (serialized, 200)
        assert await delivery.next() is None
        assert delivery.peak_bytes == 0

    asyncio.run(deliver())
    path = tmp_path / "history.jsonl"
    append_history_turn(
        path,
        conversation_id="maximum",
        request_id="maximum",
        question="Synthetic maximum ranks",
        provider="fixture",
        model="none",
        payload=p,
    )
    restored = read_history(path)["conversations"][0]["turns"][0]["payload"]
    assert restored == json.loads(serialized)
    assert validate(restored)["errors"] == []


@pytest.mark.parametrize(
    "mutation",
    [
        "boolean_ref",
        "dangling_ref",
        "cycle",
        "forward_parent",
        "boolean_parent",
        "duplicate_path",
        "duplicate_descriptor",
        "duplicate_transform",
        "extra_registry",
        "malformed_claim",
        "boolean_token",
    ],
)
def test_compact_registry_mutations_fail_after_json_roundtrip(mutation):
    payload = json.loads(json.dumps(literal_breakdown()))
    assert validate(payload)["errors"] == []
    manifest = payload["claim_bindings"]
    claim = value_claim(payload)
    if mutation == "boolean_ref":
        claim["i"] = False
    elif mutation == "dangling_ref":
        claim["m"] = len(manifest["descriptors"])
    elif mutation == "cycle":
        manifest["paths"][0][0] = 0
    elif mutation == "forward_parent":
        manifest["paths"][0][0] = 1
    elif mutation == "boolean_parent":
        manifest["paths"][1][0] = False
    elif mutation == "boolean_token":
        manifest["paths"][0][1] = False
    elif mutation.startswith("duplicate_"):
        registry = {
            "duplicate_path": "paths",
            "duplicate_descriptor": "descriptors",
            "duplicate_transform": "transforms",
        }[mutation]
        manifest[registry].append(copy.deepcopy(manifest[registry][0]))
    elif mutation == "extra_registry":
        manifest["unknown"] = []
    elif mutation == "malformed_claim":
        claim["expected_display"] = 5.0
    assert validate(payload)["status"] == "invalid"
    assert finalize(payload, mode="enforce")["status"] == "error"


def test_small_summary_compact_metadata_budget():
    payload = literal_breakdown()
    assert len(json.dumps(payload["claim_bindings"]).encode()) < 3000
    restored = json.loads(json.dumps(payload))
    assert validate(restored)["errors"] == []


def test_supported_single_rank_plan_size_and_roundtrip():
    payload = maximum_rank_payload(single_query=True)
    assert payload["status"] == "ok"
    assert len(payload["tables"]) == 1
    assert len(payload["tables"][0]["rows"]) == 100
    assert len(json.dumps(payload).encode()) < 150_000
    assert len(json.dumps(payload["claim_bindings"]).encode()) < 100_000
    assert validate(json.loads(json.dumps(payload)))["errors"] == []


def test_transform_registry_preserves_exact_scalar_types():
    payload = maximum_rank_payload(single_query=True)
    manifest = payload["claim_bindings"]
    numeric = next(spec for spec in manifest["transforms"] if "digits" in spec)
    numeric["digits"] = float(numeric["digits"])
    assert validate(payload)["status"] == "invalid"


# Public orchestration tests use authored plans through the provider contract.
pytestmark = pytest.mark.usefixtures("scripted_intelligence")
