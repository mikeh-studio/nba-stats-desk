"""Governed playoff scopes and verified position cohorts."""

import json
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest
from app.agent.ask_data_bundle import AskDataBundle, record_bundle_shadow
from app.agent.claims import expand_path, validate
from app.agent.historical_positions import GROUPS
from app.agent.overview_percentiles import add_percentiles
from app.agent.overview_positions import add_position_comparisons, validated_context
from app.agent.publication_ask_source import PublicationAskSource
from app.agent.scope_policy import requested_playoff_round
from app.agent.semantics import (
    PLAYOFF_ROUNDS,
    Evidence,
    Query,
    SemanticError,
    playoff_round_from_game_id,
    run_query,
)
from app.agent.service import StatsAgent
from tests.test_ask_data_bundle import CORE_EXPIRY, NOW
from tests.test_overview_positions import fixture as position_fixture
from tests.test_semantic_migration import Model, Warehouse, settings

SEASON = "2024-25"
PLAYOFFS = "Playoffs"
REGULAR = "Regular Season"
FIRST = (SEASON, PLAYOFFS)
LAST_DAY = "2025-06-08"


@pytest.mark.parametrize(
    "game_id,expected",
    [
        (f"0042400{number}01", round_key)
        for number, round_key in enumerate(PLAYOFF_ROUNDS, start=1)
    ]
    + [
        ("0052400121", None),
        ("0022400121", None),
        ("0042400100", None),
        ("0042400108", None),
        ("0042400501", None),
        ("0042401101", None),
        ("0042400101\n", None),
        ("42400101", None),
        (42400101, None),
        ("2025-04-19_BOS_NYK", None),
        (None, None),
    ],
)
def test_canonical_playoff_id(game_id, expected):
    assert playoff_round_from_game_id(game_id) == expected


@pytest.mark.parametrize(
    "wording,key",
    [
        ("first round", "first_round"),
        ("1st round", "first_round"),
        ("opening round", "first_round"),
        ("conference semifinals", "conference_semifinals"),
        ("conference semis", "conference_semifinals"),
        ("second round", "conference_semifinals"),
        ("2nd round", "conference_semifinals"),
        ("conference finals", "conference_finals"),
        ("third round", "conference_finals"),
        ("3rd round", "conference_finals"),
        ("nba finals", "nba_finals"),
        ("finals", "nba_finals"),
        ("early playoff rounds", None),
        ("first round and Finals", None),
        ("regular season vs Finals", None),
        ("the final score", None),
        ("excluding the first round", None),
        ("before the Finals", None),
    ],
)
def test_round_wording(wording, key):
    assert requested_playoff_round(wording) == key


def round_rows():
    rows = []
    for game_id, day, phase, points, metadata in (
        ("0022400121", "2025-01-01", REGULAR, 90, {"playoff_round": "first_round"}),
        ("0042400101", "2025-04-19", PLAYOFFS, 12, {}),
        (
            "derived-playoff",
            "2025-04-21",
            PLAYOFFS,
            30,
            {"playoff_round": "first_round"},
        ),
        # Explicit warehouse metadata takes precedence over the encoded round.
        (
            "0042400103",
            "2025-05-06",
            PLAYOFFS,
            50,
            {"playoff_round_name": "Conference Semifinals"},
        ),
        ("0042400301", "2025-05-22", PLAYOFFS, 71, {}),
        ("0042400401", LAST_DAY, PLAYOFFS, 99, {}),
    ):
        rows.append(
            dict(
                player_id=1,
                player_name="Regular Player",
                season=SEASON,
                game_id=game_id,
                game_date=day,
                season_type=phase,
                pts=points,
                min=32,
                team_abbr="BOS",
                opponent_abbr="NYK",
                **metadata,
            )
        )
    return rows


def round_evidence(rows):
    return Evidence(
        rows,
        frozenset({FIRST, (SEASON, REGULAR)}),
        "fixtures/player-games",
        "p4a-fixture",
        {FIRST: LAST_DAY, (SEASON, REGULAR): "2025-01-01"},
        complete=True,
    )


def test_round_filter_precedes_appearance_window_and_qualifying_game_counts():
    evidence = round_evidence(round_rows())
    query = Query(
        "pts",
        SEASON,
        "average",
        season_type=PLAYOFFS,
        player_id=1,
        playoff_round="first_round",
    )
    result = run_query(evidence, query)
    [summary] = result["rows"]
    assert result["scope"]["playoff_round"] == "first_round"
    assert summary["game_ids"] == ["derived-playoff", "0042400101"]
    assert (summary["value"], summary["numerator"], summary["denominator"]) == (
        21,
        42,
        2,
    )
    [latest] = run_query(evidence, replace(query, window="last_n_games", n=1))["rows"]
    assert latest["value"] == 30
    assert latest["required_games"] == 1


@pytest.mark.parametrize(
    "phase,round_key,code",
    [
        (REGULAR, "first_round", "invalid_scope"),
        ("Both", "first_round", "invalid_scope"),
        (PLAYOFFS, "unknown_round", "invalid_scope"),
        (PLAYOFFS, "first_round", "unsupported_scope"),
    ],
)
def test_round_scope_fails_closed(phase, round_key, code):
    evidence = round_evidence(
        [{**round_rows()[1], "game_id": "unmapped", "playoff_round": "unrecognized"}]
    )
    with pytest.raises(SemanticError) as err:
        run_query(
            evidence,
            Query("pts", SEASON, "total", season_type=phase, playoff_round=round_key),
        )
    assert err.value.code == code


def ask_fixture(rows, *, claim_mode="enforce"):
    config = replace(settings(), ask_claim_enforcement=claim_mode)
    warehouse = Warehouse()
    warehouse.rows = rows
    warehouse.coverage = [
        (*FIRST, LAST_DAY),
        (SEASON, REGULAR, "2025-01-01"),
    ]
    model = Model(season_type=PLAYOFFS, aggregation="total")
    repo = SimpleNamespace(settings=config, _governed_warehouse=warehouse)
    return (
        StatsAgent(config, repo, client=model, semantic_warehouse=warehouse),
        model,
        warehouse,
    )


@pytest.mark.parametrize(
    "question,shape",
    [
        (
            "How many total points did Regular Player score in the 2024-25 first round?",
            "governed_metrics",
        ),
        (
            "How did Regular Player perform in the 2024-25 opening round?",
            "player_overview",
        ),
    ],
)
def test_dispatch_answers_and_binds_verified_round(question, shape):
    ask, _, warehouse = ask_fixture(round_rows())
    result = ask.answer(question)
    assert result["status"] == "ok", result["answer"]
    assert result["ask_route"]["route"] == shape
    evidence = result["semantic_evidence"]
    assert evidence["scope"]["playoff_round"] == "first_round"
    assert any("First Round" in assumption for assumption in result["assumptions"])
    assert warehouse.calls == [[SEASON]]
    if shape == "governed_metrics":
        assert result["claim_validation"]["status"] != "invalid"
        assert evidence["rows"][0]["value"] == 42
        bindings = result["claim_bindings"]
        title = next(
            c
            for c in bindings["claims"]
            if expand_path(bindings, c["d"]) == ["tables", 0, "title"]
        )
        assert ["semantic_evidence", "scope", "playoff_round"] in [
            expand_path(bindings, src) for src in title["s"]
        ]
        assert validate(result)["errors"] == []
    else:
        assert set(evidence["game_ids"]) == {"0042400101", "derived-playoff"}


def test_dispatch_withholds_unbacked_round_and_runs_participation_first():
    ask, model, warehouse = ask_fixture([{**round_rows()[1], "game_id": "unmapped"}])
    refusal = ask.answer("How did Regular Player perform in the 2024-25 first round?")
    assert refusal["status"] == "unsupported_scope"
    assert refusal["ask_route"]["reason_code"] == "unsupported_round"
    assert not refusal["semantic_evidence"]
    vague = ask.answer("How did Regular Player perform in the early playoff rounds?")
    assert vague["ask_route"]["reason_code"] == "unsupported_round"
    known = ask.answer(
        "How many points did Stephen Curry score in the 2025-26 NBA Finals?"
    )
    assert known["status"] == "not_applicable"
    assert known["ask_route"]["route"] == "event_participation"
    assert not model.calls
    assert warehouse.calls == [[SEASON]]


def season_positions(roster):
    return [
        dict(
            player_id=r["player_id"],
            season=r["season"],
            listed_position=r["position"],
            position_groups=GROUPS[r["position"]],
            position_source=r["provenance"],
            source_ref=r["source_ref"],
            is_verified_season_roster=True,
        )
        for r in roster["records"]
    ]


def core_source(evidence):
    playoff_rows = [r for r in evidence.rows if r["season_type"] == PLAYOFFS]
    return PublicationAskSource(
        replace(
            evidence,
            rows=playoff_rows,
            covered_scopes=frozenset({FIRST}),
            data_through={FIRST: LAST_DAY},
        ),
        dict(
            publication_id="core_fixture",
            freshness="fresh",
            scope=dict(
                season=SEASON, phase=PLAYOFFS, first="2025-04-19", last=LAST_DAY
            ),
        ),
        expires_at=CORE_EXPIRY.isoformat(),
    )


def test_bundle_declares_verified_capabilities_and_binds_immutable_positions():
    _, legacy = position_fixture()
    records = season_positions(legacy)
    source = core_source(round_evidence(round_rows()))
    bundle = AskDataBundle.from_core_publication(
        source, now=NOW, metrics=["pts"], season_positions=records
    )
    assert {"playoff_rounds", "season_positions"} <= set(
        bundle.manifest["capabilities"]
    )
    slot = bundle.manifest["auxiliary"]["season_positions"]
    assert slot["record_count"] == 20
    assert bundle.evidence.position_context["sha256"] == slot["digest"]
    records[0]["listed_position"] = "C"
    assert bundle.auxiliary["season_positions"][0]["listed_position"] == "PG"
    assert validated_context(bundle.evidence.position_context) == validated_context(
        legacy
    )
    retained = replace(
        source.evidence,
        position_context={"season_positions": season_positions(legacy)},
    )
    assert record_bundle_shadow(lambda: bundle.evidence, retained) == {
        "status": "match"
    }
    bad = AskDataBundle.from_core_publication(
        source, now=NOW, metrics=["pts"], season_positions=records
    )
    assert "season_positions" not in bad.manifest["capabilities"]


@pytest.mark.parametrize(
    "size,mode", [(9, "withheld"), (10, "ordinal"), (20, "percentile")]
)
def test_position_cohort_uses_the_same_membership_and_thresholds(size, mode):
    grouped, legacy = position_fixture(size)
    records = season_positions(legacy)
    scope = dict(start=date(2024, 7, 1), end=date(2025, 6, 30), phases=[REGULAR])

    def compare(context):
        metrics = [
            dict(
                key="pts",
                value=1,
                valid_games=5,
                missing_component_games=0,
                components={},
            )
        ]
        add_percentiles(metrics, grouped, 1, 5)
        add_position_comparisons(metrics, grouped, 1, scope, context, "among guards")
        return metrics[0]["peer_comparison"]

    gold = compare({"season_positions": records})
    assert gold["mode"] == mode
    assert gold["membership_sha256"] == compare(legacy)["membership_sha256"]
    another_season = [{**row, "season": "2025-26"} for row in records]
    assert (
        compare({"season_positions": another_season})["fallback_reason"]
        == "missing_historical_provenance"
    )
    if size == 20:
        records.pop()
        assert compare({"season_positions": records})["coverage"] == 0.95
        assert compare({"season_positions": records})["kind"] == "position"
        records.pop()
        assert (
            compare({"season_positions": records})["fallback_reason"]
            == "insufficient_coverage"
        )


@pytest.mark.parametrize(
    "change",
    [
        {"position_source": "historical_copy"},
        {"position_source": "current_player_reference"},
        {"is_verified_season_roster": False},
        {"position_groups": ["Big"]},
        {"season": "2025-25"},
    ],
)
def test_positions_reject_invalid_or_unverified_provenance(change):
    _, legacy = position_fixture()
    records = season_positions(legacy)
    records[0].update(change)
    assert validated_context({"season_positions": records}) == ({}, set())


def test_json_and_sse_apply_the_same_round_filter(monkeypatch):
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository
    from fastapi.testclient import TestClient

    _, model, warehouse = ask_fixture(round_rows())
    config = settings()
    repository = BigQueryWarehouseRepository(config, client=SimpleNamespace())
    repository._governed_warehouse = warehouse
    monkeypatch.setitem(app.dependency_overrides, get_settings, lambda: config)
    monkeypatch.setitem(app.dependency_overrides, get_repository, lambda: repository)
    monkeypatch.setitem(app.dependency_overrides, get_agent_client, lambda: model)
    body = {"question": "Regular Player total points in 2024-25 opening round"}
    with TestClient(app) as client:
        direct = client.post("/api/agent/ask?season=2024-25", json=body)
        assert direct.status_code == 200
        stream = client.post("/api/agent/ask/stream?season=2024-25", json=body)
        assert stream.status_code == 200
    final = next(
        event["payload"]
        for line in stream.text.splitlines()
        if line.startswith("data: ")
        and (event := json.loads(line[6:])).get("type") == "final"
    )
    for payload in (direct.json(), final):
        assert payload["status"] == "ok", payload["answer"]
        assert payload["semantic_evidence"]["scope"]["playoff_round"] == "first_round"
        assert payload["semantic_evidence"]["rows"][0]["value"] == 42
        assert payload["claim_validation"]["status"] != "invalid"
