"""Governed playoff scopes and verified position cohorts."""

import json
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest
from app.agent.ask_data_bundle import AskDataBundle, record_bundle_shadow
from app.agent.claims import expand_path, validate
from app.agent.conversation import InMemoryConversationStore
from app.agent.historical_positions import GROUPS, MAPPING_VERSION, digest
from app.agent.overview_percentiles import add_percentiles
from app.agent.overview_positions import add_position_comparisons, validated_context
from app.agent.performance_overview import build_overview
from app.agent.publication_ask_source import PublicationAskSource
from app.agent.scope_policy import (
    normalize_playoff_round_year,
    playoff_round_planning_question,
    requested_playoff_round,
    round_scope_message,
)
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
        ("Eastern Conference Semifinals", None),
        ("West Conference Semis", None),
        ("second round", "conference_semifinals"),
        ("2nd round", "conference_semifinals"),
        ("conference finals", "conference_finals"),
        ("Eastern Conference Finals", None),
        ("Western Conference Finals", None),
        ("third round", "conference_finals"),
        ("3rd round", "conference_finals"),
        ("nba finals", "nba_finals"),
        ("finals", "nba_finals"),
        ("Finals of 2024", "nba_finals"),
        ("NBA Finals 2024", "nba_finals"),
        ("David West in the 2024 NBA Finals", "nba_finals"),
        ("first-round pick in the NBA Finals", "nba_finals"),
        ("first-round pick", None),
        ("first round pick", None),
        ("1st-round pick", None),
        ("early playoff rounds", None),
        ("first round and Finals", None),
        ("regular season vs Finals", None),
        ("the final score", None),
        ("excluding the first round", None),
        ("excluding the Finals", None),
        ("except the Finals", None),
        ("not counting the NBA Finals", None),
        ("before the Finals", None),
        ("outside the Eastern Conference Finals", None),
        ("before the 2024 Western Conference Finals", None),
        ("Finals opener", None),
        ("series opener", None),
        ("game opener", None),
        ("games 1 and 2 of the first round", None),
        ("games 1-3 of the Finals", None),
        ("first two games of the NBA Finals", None),
    ],
)
def test_round_wording(wording, key):
    assert requested_playoff_round(wording) == key


@pytest.mark.parametrize("season", ["2024-25", "2024-2025", "2024/2025", "2024–2025"])
def test_round_year_normalization_preserves_named_season(season):
    question = f"points in the {season} NBA Finals"
    assert normalize_playoff_round_year(question) == question


def test_round_year_normalization_and_planning_rewrite():
    question = (
        "How many total points did Luka Doncic score in the 2024 Conference Finals?"
    )
    normalized = normalize_playoff_round_year(question)
    assert (
        normalized
        == "How many total points did Luka Doncic score in the 2023-24 Conference Finals?"
    )
    assert (
        playoff_round_planning_question(normalized)
        == "Luka Doncic total points in the 2023-24 playoffs?"
    )
    assert (
        playoff_round_planning_question(
            "How many total points did Luka Doncic score in the Conference Semifinals?",
            season="2023-24",
        )
        == "Luka Doncic total points in the 2023-24 playoffs?"
    )
    assert (
        normalize_playoff_round_year("points in the Finals of 2024")
        == "points in the Finals of 2023-24"
    )
    assert (
        normalize_playoff_round_year("points in the NBA Finals 2024")
        == "points in the NBA Finals 2023-24"
    )
    conference_q = "How many total points did Luka Doncic score in the 2024 Western Conference Finals?"
    assert requested_playoff_round(conference_q) is None
    assert round_scope_message(conference_q) is not None


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


def test_unclassified_appearance_cannot_undercount_a_round_total():
    rows = round_rows()
    query = Query(
        "pts",
        SEASON,
        "total",
        season_type=PLAYOFFS,
        player_id=1,
        playoff_round="first_round",
    )
    assert run_query(round_evidence(rows), query)["rows"][0]["value"] == 42
    del rows[2]["playoff_round"]
    with pytest.raises(SemanticError) as err:
        run_query(round_evidence(rows), query)
    assert err.value.code == "unsupported_scope"

    before_unclassified = replace(query, as_of="2025-04-19")
    assert (
        run_query(round_evidence(rows), before_unclassified)["rows"][0]["value"] == 12
    )
    rows[2].update(player_id=2, player_name="Another Player")
    peer_incomplete = run_query(round_evidence(rows), query)
    assert peer_incomplete["cohort_complete"] is False
    assert peer_incomplete["cohort"]["cohort_complete"] is False
    assert peer_incomplete["rows"][0]["value"] == 12
    assert peer_incomplete["rows"][0]["rank"] is None
    assert peer_incomplete["rows"][0]["percentile"] is None


def test_out_of_scope_predicate_row_does_not_block_verified_round_total():
    rows = [
        {**round_rows()[1], "home_away": "HOME", "pts": 20},
        {
            **round_rows()[1],
            "game_id": "unmapped-away",
            "home_away": "AWAY",
            "pts": 15,
        },
    ]
    home_query = Query(
        "pts",
        SEASON,
        "total",
        season_type=PLAYOFFS,
        player_id=1,
        home_away="HOME",
        playoff_round="first_round",
    )
    result = run_query(round_evidence(rows), home_query)
    assert result["rows"][0]["value"] == 20
    assert result["rows"][0]["game_ids"] == ["0042400101"]
    with pytest.raises(SemanticError) as err:
        run_query(round_evidence(rows), replace(home_query, home_away=None))
    assert err.value.code == "unsupported_scope"


def test_incomplete_peer_round_withholds_cohort_claims_and_overview_percentiles():
    rows = round_rows()
    del rows[2]["playoff_round"]
    rows[2].update(player_id=2, player_name="Another Player")
    ask, _, _ = ask_fixture(rows)
    result = ask.answer(
        "How many total points did Regular Player score in the 2024-25 first round?"
    )
    assert result["status"] == "ok", result["answer"]
    evidence = result["semantic_evidence"]
    assert evidence["cohort_complete"] is False
    assert evidence["rows"][0]["value"] == 12
    assert evidence["rows"][0]["rank"] is None
    assert evidence["rows"][0]["percentile"] is None
    assert not any(note.startswith("Ranking cohort:") for note in result["assumptions"])
    assert validate(result)["errors"] == []

    overview = build_overview(
        "How did Regular Player perform in the 2024-25 first round?",
        round_evidence(rows),
        [{"player_id": 1, "player_name": "Regular Player"}],
        {
            "start": date(2024, 7, 1),
            "end": date(2025, 6, 30),
            "previous_start": date(2024, 7, 1),
            "previous_end": date(2025, 6, 30),
            "phases": [PLAYOFFS],
            "previous_phases": [REGULAR],
            "seasons": [SEASON],
            "playoff_round": "first_round",
        },
    )
    assert overview["status"] == "ok"
    assert overview["semantic_evidence"]["cohort_complete"] is False
    assert overview["semantic_evidence"]["metrics"][0]["percentile"] is None


def ask_fixture(rows, *, claim_mode="enforce", conversation_store=None):
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
        StatsAgent(
            config,
            repo,
            client=model,
            conversation_store=conversation_store,
            semantic_warehouse=warehouse,
        ),
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


@pytest.mark.parametrize(
    "question,game_id,round_key",
    [
        (
            "How many total points did Regular Player score in the 2024 NBA Finals?",
            "0042300401",
            "nba_finals",
        ),
        (
            "How many total points did Regular Player score in the Finals of 2024?",
            "0042300401",
            "nba_finals",
        ),
        (
            "How many total points did Regular Player score in the NBA Finals 2024?",
            "0042300401",
            "nba_finals",
        ),
        (
            "How did Regular Player perform in the 2024 Conference Finals?",
            "0042300301",
            "conference_finals",
        ),
        (
            "How many total points did Regular Player score in the 2024 Conference Semifinals?",
            "0042300201",
            "conference_semifinals",
        ),
    ],
)
@pytest.mark.parametrize("archived", [True, False])
def test_ask_named_round_year_uses_only_the_requested_archive(
    question, game_id, round_key, archived
):
    rows = round_rows()
    ask, _, warehouse = ask_fixture(rows)
    if archived:
        rows.append(
            dict(
                rows[-1],
                season="2023-24",
                game_id=game_id,
                game_date="2024-06-09",
                pts=17,
            )
        )
        warehouse.coverage.append(("2023-24", PLAYOFFS, "2024-06-09"))
    result = ask.answer(question)
    assert warehouse.calls == [["2023-24"]]
    if not archived:
        assert result["status"] in ("unsupported_coverage", "unsupported_scope")
        assert not result["semantic_evidence"]
        return
    assert result["status"] == "ok", result["answer"]
    evidence = result["semantic_evidence"]
    assert evidence["scope"]["playoff_round"] == round_key
    if "rows" in evidence:
        assert evidence["rows"][0]["value"] == 17
        assert evidence["rows"][0]["game_ids"] == [game_id]
    else:
        assert evidence["game_ids"] == [game_id]


def season_positions(roster):
    return [
        dict(
            player_id=r["player_id"],
            season=r["season"],
            listed_position=r["position"],
            position_groups=GROUPS[r["position"]],
            position_source=r["provenance"],
            source_ref=r["source_ref"],
            source_sha256=r["source_sha256"],
            source_locator=r["source_locator"],
            source_date=r["source_date"],
            is_verified_season_roster=True,
        )
        for r in roster["records"]
    ]


def position_context(records, approved_seasons=(SEASON,)):
    context = dict(
        mapping_version=MAPPING_VERSION,
        approved_seasons=list(approved_seasons),
        season_positions=records,
    )
    context["sha256"] = digest(context)
    return context


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
    context = position_context(records)
    source = core_source(round_evidence(round_rows()))
    bundle = AskDataBundle.from_core_publication(
        source, now=NOW, metrics=["pts"], season_positions=context
    )
    assert {"playoff_rounds", "season_positions"} <= set(
        bundle.manifest["capabilities"]
    )
    slot = bundle.manifest["auxiliary"]["season_positions"]
    assert slot["record_count"] == 20
    assert bundle.evidence.position_context["sha256"] == context["sha256"]
    records[0]["listed_position"] = "C"
    assert bundle.auxiliary["season_positions"][0]["listed_position"] == "PG"
    assert validated_context(bundle.evidence.position_context) == validated_context(
        legacy
    )
    retained = replace(
        source.evidence,
        position_context=position_context(season_positions(legacy)),
    )
    assert record_bundle_shadow(lambda: bundle.evidence, retained) == {
        "status": "match"
    }
    bad = AskDataBundle.from_core_publication(
        source, now=NOW, metrics=["pts"], season_positions=context
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

    gold = compare(position_context(records))
    assert gold["mode"] == mode
    assert gold["membership_sha256"] == compare(legacy)["membership_sha256"]
    another_season = [{**row, "season": "2025-26"} for row in records]
    assert (
        compare(position_context(another_season, ["2025-26"]))["fallback_reason"]
        == "missing_historical_provenance"
    )
    if size == 20:
        records.pop()
        assert compare(position_context(records))["coverage"] == 0.95
        assert compare(position_context(records))["kind"] == "position"
        records.pop()
        assert (
            compare(position_context(records))["fallback_reason"]
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
        {"source_ref": ""},
        {"source_sha256": None},
        {"source_date": "2025-02-30"},
        {"source_locator": " "},
    ],
)
def test_positions_reject_invalid_or_unverified_provenance(change):
    _, legacy = position_fixture()
    records = season_positions(legacy)
    records[0]["position_source"] = "dated_team_document"
    assert validated_context(position_context(records)) == validated_context(legacy)
    records[0].update(change)
    assert validated_context(position_context(records)) == ({}, set())


def test_position_context_rejects_tampered_or_unapproved_records():
    _, legacy = position_fixture()
    records = season_positions(legacy)
    context = position_context(records)
    changed = [{**records[0], "source_locator": "another roster row"}, *records[1:]]
    unapproved = position_context(records, [SEASON, "2025-26"])
    unapproved["season_positions"] = [{**records[0], "season": "2023-24"}, *records[1:]]
    unapproved["sha256"] = digest(unapproved)
    source = core_source(round_evidence(round_rows()))
    for invalid in (
        {"season_positions": records},
        {**context, "season_positions": changed},
        unapproved,
    ):
        assert validated_context(invalid) == ({}, set())
        bundle = AskDataBundle.from_core_publication(
            source, now=NOW, metrics=["pts"], season_positions=invalid
        )
        assert "season_positions" not in bundle.manifest["capabilities"]


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


def test_browser_restored_followup_preserves_and_revalidates_playoff_round(monkeypatch):
    from app import main as main_module
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository
    from fastapi.testclient import TestClient

    rows = [
        dict(row, home_away="HOME" if index % 2 == 0 else "AWAY", ast=10)
        for index, row in enumerate(round_rows())
    ]
    _, model, warehouse = ask_fixture(rows)
    config = settings()
    repository = BigQueryWarehouseRepository(config, client=SimpleNamespace())
    repository._governed_warehouse = warehouse
    store = InMemoryConversationStore()
    monkeypatch.setattr(
        main_module,
        "_season_conversation_store",
        lambda season: store,
    )
    monkeypatch.setitem(app.dependency_overrides, get_settings, lambda: config)
    monkeypatch.setitem(app.dependency_overrides, get_repository, lambda: repository)
    monkeypatch.setitem(app.dependency_overrides, get_agent_client, lambda: model)
    with TestClient(app) as client:
        first = client.post(
            "/api/agent/ask?season=2024-25",
            json={"question": "Regular Player total points in 2024-25 first round"},
        )
        assert first.status_code == 200
        first_payload = first.json()
        assert first_payload["semantic_evidence"]["scope"]["playoff_round"] == (
            "first_round"
        )
        # Simulate browser reload with fresh server conversation store.
        store = InMemoryConversationStore()
        prior_context = {
            "question": "Regular Player total points in 2024-25 first round",
            "players": [{"player_id": 1, "player_name": "Regular Player"}],
            "scope": {
                **first_payload["semantic_evidence"]["scope"],
                "phases": [PLAYOFFS],
            },
            "metrics": ["pts"],
        }
        restored = client.post(
            "/api/agent/ask?season=2024-25",
            json={
                "question": "What about total assists?",
                "conversation_id": "restored-round-chat",
                "previous_context": prior_context,
            },
        )
        assert restored.status_code == 200
        restored_payload = restored.json()
        assert restored_payload["status"] == "ok", restored_payload["answer"]
        assert restored_payload["semantic_evidence"]["metric"]["key"] == "ast"
        assert restored_payload["semantic_evidence"]["rows"][0]["value"] == 20
        assert restored_payload["semantic_evidence"]["scope"]["playoff_round"] == (
            "first_round"
        )

        # Browser recovery preserves round scope without inventing missing aggregation.
        store = InMemoryConversationStore()
        ambiguous = client.post(
            "/api/agent/ask?season=2024-25",
            json={
                "question": "What about assists?",
                "conversation_id": "ambiguous-round-chat",
                "previous_context": prior_context,
            },
        )
        assert ambiguous.status_code == 200
        assert ambiguous.json()["status"] == "clarification_required"

        invalid_round = client.post(
            "/api/agent/ask?season=2024-25",
            json={
                "question": "What about total assists?",
                "conversation_id": "bad-round-chat",
                "previous_context": {
                    **prior_context,
                    "scope": {**prior_context["scope"], "playoff_round": "bogus"},
                },
            },
        )
        assert invalid_round.status_code == 422

        mismatched_phase = client.post(
            "/api/agent/ask?season=2024-25",
            json={
                "question": "What about total assists?",
                "conversation_id": "mismatched-phase-chat",
                "previous_context": {
                    **prior_context,
                    "scope": {
                        **prior_context["scope"],
                        "season_type": REGULAR,
                        "phases": [REGULAR],
                    },
                },
            },
        )
        assert mismatched_phase.status_code == 422

        # Revalidation against warehouse refuses if round coverage disappeared.
        warehouse.rows = [{**rows[1], "game_id": "unmapped"}]
        store = InMemoryConversationStore()
        unbacked = client.post(
            "/api/agent/ask?season=2024-25",
            json={
                "question": "What about total assists?",
                "conversation_id": "unbacked-round-chat",
                "previous_context": prior_context,
            },
        )
        assert unbacked.status_code == 200
        assert unbacked.json()["status"] == "unsupported_scope"


def test_landing_contracts_do_not_widen_game_logs_or_schedule():
    import sys
    from pathlib import Path

    import pandas as pd
    from tests.test_source_contracts import _game_log_row, _schedule_rows

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dags"))
    import nba_source_contracts as contracts

    game_logs = pd.DataFrame([_game_log_row()])
    validated_logs = contracts.validate_source_contract(
        "game_logs", game_logs, season="2025-26"
    )
    assert len(validated_logs.frame.columns) == 29
    assert list(validated_logs.frame.columns) == list(game_logs.columns)

    schedule = pd.DataFrame(_schedule_rows())
    validated_schedule = contracts.validate_source_contract(
        "schedule", schedule, season="2025-26"
    )
    assert len(validated_schedule.frame.columns) == 11
    assert list(validated_schedule.frame.columns) == [
        *schedule.columns,
        "GAME_TIME_UTC",
    ]


def test_historical_round_followup_overview_uses_inherited_season():
    rows = round_rows()
    rows.append(
        dict(
            rows[1],
            season="2023-24",
            game_id="0042300101",
            game_date="2024-04-20",
            pts=25,
        )
    )
    rows.append(
        dict(
            rows[1],
            season="2023-24",
            game_id="0042300401",
            game_date="2024-06-09",
            pts=40,
        )
    )
    ask, _, warehouse = ask_fixture(
        rows, conversation_store=InMemoryConversationStore()
    )
    warehouse.coverage.append(("2023-24", PLAYOFFS, "2024-06-09"))
    first = ask.answer(
        "How many total points did Regular Player score in the 2023-24 first round?",
        conversation_id="hist-round-followup",
    )
    assert first["status"] == "ok", first["answer"]
    assert first["semantic_evidence"]["rows"][0]["value"] == 25
    warehouse.calls.clear()

    followup = ask.answer(
        "How did he perform?",
        conversation_id="hist-round-followup",
    )
    assert followup["status"] == "ok", followup["answer"]
    assert followup["ask_route"]["route"] == "player_overview"
    assert warehouse.calls == [["2023-24"]]
    assert followup["semantic_evidence"]["scope"]["seasons"] == ["2023-24"]
    assert followup["semantic_evidence"]["scope"]["playoff_round"] == "first_round"
    assert followup["semantic_evidence"]["game_ids"] == ["0042300101"]

    warehouse.calls.clear()
    explicit_new = ask.answer(
        "How did he perform in 2024-25?",
        conversation_id="hist-round-followup",
    )
    assert explicit_new["status"] == "ok", explicit_new["answer"]
    assert warehouse.calls == [["2024-25"]]
    assert explicit_new["semantic_evidence"]["scope"]["seasons"] == ["2024-25"]


def test_leaderboard_followup_carries_playoff_round_and_revalidates():
    rows = [dict(row, home_away="HOME") for row in round_rows()]
    rows.extend(
        [
            dict(
                rows[1],
                player_id=2,
                player_name="Another Player",
                team_abbr="NYK",
                opponent_abbr="BOS",
                game_id="0042400101",
                game_date="2025-04-19",
                pts=35,
            ),
            dict(
                rows[1],
                player_id=2,
                player_name="Another Player",
                team_abbr="NYK",
                opponent_abbr="BOS",
                game_id="0042400102",
                game_date="2025-04-20",
                pts=35,
            ),
            dict(
                rows[1],
                player_id=2,
                player_name="Another Player",
                team_abbr="NYK",
                opponent_abbr="BOS",
                game_id="0042400402",
                game_date="2025-06-08",
                pts=90,
            ),
        ]
    )
    ask, _, warehouse = ask_fixture(
        rows, conversation_store=InMemoryConversationStore()
    )
    first = ask.answer(
        "How many total points did Regular Player score in the 2024-25 first round?",
        conversation_id="round-leader-chat",
    )
    assert first["status"] == "ok"
    leaders = ask.answer("Who leads in points?", conversation_id="round-leader-chat")
    assert leaders["status"] == "ok", leaders["answer"]
    assert leaders["semantic_evidence"]["scope"]["playoff_round"] == "first_round"
    assert leaders["semantic_evidence"]["scope"]["season_type"] == PLAYOFFS
    # First-round average for Another Player is 35.0, not 62.5 across all playoffs.
    assert leaders["semantic_evidence"]["rows"][0]["player_id"] == 2
    assert leaders["semantic_evidence"]["rows"][0]["value"] == 35

    # Coverage revalidation fails closed if round metadata disappears before follow-up.
    warehouse.rows = [{**rows[1], "game_id": "unmapped"}]
    unbacked = ask.answer("Who leads in points?", conversation_id="round-leader-chat")
    assert unbacked["status"] == "unsupported_scope"


def test_conflict_withheld_rows_do_not_reconstruct_round_from_game_id():
    from app.agent.semantics import row_playoff_round
    from app.agent.source_policy import capture_sql

    sql = capture_sql(["test-project.nba_gold.fct_player_game_stats"], [PLAYOFFS])
    assert "playoff_round_source" in sql
    assert "playoff_round_conflict" in sql
    assert "has_playoff_round_column" in sql

    assert (
        row_playoff_round(
            {
                "season_type": PLAYOFFS,
                "game_id": "0042400101",
                "playoff_round": None,
                "playoff_round_source": "conflict",
            }
        )
        is None
    )
    assert (
        row_playoff_round(
            {
                "season_type": PLAYOFFS,
                "game_id": "0042400101",
                "playoff_round": None,
                "playoff_round_conflict": True,
            }
        )
        is None
    )
    assert (
        row_playoff_round(
            {
                "season_type": PLAYOFFS,
                "game_id": "0042400101",
                "playoff_round": None,
                "has_playoff_round_column": True,
            }
        )
        is None
    )
    # Legacy schema without round column still allows canonical game_id fallback.
    assert (
        row_playoff_round(
            {
                "season_type": PLAYOFFS,
                "game_id": "0042400101",
                "playoff_round": None,
                "has_playoff_round_column": False,
            }
        )
        == "first_round"
    )


def test_round_plus_venue_excludes_other_rounds_and_flags_unknown_peer_venue():
    rows = [
        {**round_rows()[1], "home_away": "HOME", "pts": 20},
        # Finals row for target player lacks venue; must not block first_round+HOME.
        {**round_rows()[-1], "home_away": None, "pts": 99},
        # Peer in first_round has unknown venue; must mark cohort_complete=False.
        {
            **round_rows()[1],
            "player_id": 2,
            "player_name": "Peer Player",
            "game_id": "0042400102",
            "home_away": None,
            "pts": 30,
        },
    ]
    result = run_query(
        round_evidence(rows),
        Query(
            "pts",
            SEASON,
            "average",
            season_type=PLAYOFFS,
            player_id=1,
            home_away="HOME",
            playoff_round="first_round",
        ),
    )
    assert result["status"] == "ok"
    assert result["rows"][0]["value"] == 20
    assert result["cohort_complete"] is False
    assert result["rows"][0]["rank"] is None
    assert result["rows"][0]["percentile"] is None


def test_round_overview_explicit_playoffs_baseline_and_long_season_formats():
    rows = round_rows()
    rows.append(
        dict(
            rows[1],
            season="2023-24",
            game_id="0042300101",
            game_date="2024-04-20",
            pts=18,
        )
    )
    ask, _, warehouse = ask_fixture(rows)
    warehouse.coverage.append(("2023-24", PLAYOFFS, "2024-06-09"))

    for wording in ("compared to the playoffs", "vs the playoffs"):
        result = ask.answer(
            f"How did Regular Player perform in the 2024-25 first round {wording}?"
        )
        assert result["status"] == "ok", result["answer"]
        ev = result["semantic_evidence"]
        assert ev["scope"]["previous_phases"] == [PLAYOFFS]
        pts = next(m for m in ev["metrics"] if m["key"] == "pts")
        # 2024-25 first_round avg = (12+30)/2 = 21.0; all Playoffs avg = (12+30+50+71+99)/5 = 52.4
        assert pts["value"] == pytest.approx(21.0)
        assert pts["previous"]["value"] == pytest.approx(52.4)
        assert pts["change"] == pytest.approx(21.0 - 52.4)

    for long_season in ("2023/2024", "2023-2024"):
        warehouse.calls.clear()
        res = ask.answer(
            f"How did Regular Player perform in the {long_season} first round?"
        )
        assert res["status"] == "ok", res["answer"]
        assert warehouse.calls == [["2023-24"]]
        assert res["semantic_evidence"]["scope"]["seasons"] == ["2023-24"]
        assert res["semantic_evidence"]["game_ids"] == ["0042300101"]


def test_optional_previous_period_round_error_does_not_suppress_primary_overview():
    rows = [
        {
            **round_rows()[1],
            "game_id": "unmapped",
            "game_date": "2025-04-18",
            "pts": 10,
        },
        {
            **round_rows()[1],
            "game_id": "0042400102",
            "game_date": "2025-04-21",
            "pts": 24,
        },
    ]
    ask, _, _ = ask_fixture(rows)
    res = ask.answer(
        "How did Regular Player perform in the 2024-25 first round from 2025-04-20 through 2025-04-25?"
    )
    assert res["status"] == "ok", res["answer"]
    ev = res["semantic_evidence"]
    assert ev["game_ids"] == ["0042400102"]
    assert ev["scope"]["baseline_coverage_available"] is False
    pts = next(m for m in ev["metrics"] if m["key"] == "pts")
    assert pts["value"] == 24
    assert pts["change"] is None
    assert any("optional comparison is unavailable" in a for a in res["assumptions"])
