"""Completed rounds can verify absence without inventing box-score observations."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
from app.agent.participation import (
    answer_known_nonparticipation,
    derive_nonparticipation_records,
    load_participation_catalog,
)
from app.agent.semantics import PLAYOFF_ROUND_LABELS, PLAYOFF_ROUNDS, Evidence
from app.repository import BigQueryWarehouseRepository
from fastapi.testclient import TestClient
from jinja2 import Environment
from tests.test_name_matching_routes import ask
from tests.test_playoff_rounds_and_positions import ask_fixture

SEASON = "2024-25"
REGULAR = "Regular Season"
PLAYOFFS = "Playoffs"
ROUND_TEAMS = (
    ("ATL", "BOS"),
    ("BKN", "CHA"),
    ("CHI", "CLE"),
    ("DAL", "DEN"),
    ("DET", "GSW"),
    ("HOU", "IND"),
    ("LAC", "LAL"),
    ("MEM", "MIA"),
)


def appearance(player_id, name, team, **fields):
    return {
        "player_id": player_id,
        "player_name": name,
        "team_abbr": team,
        "season": SEASON,
        "season_type": REGULAR,
        "game_date": "2025-01-01",
        "game_id": "regular-game",
        "min": 32,
        "pts": 24,
        **fields,
    }


def completed_evidence(number=4, *, explicit=False):
    rows = [
        appearance(1, "Avery Finch", "NYK"),
        appearance(2, "Blake Reed", "BOS"),
    ]
    # Reed's team reached the round, so his own missing appearances prove nothing.
    for series, (winner, loser) in enumerate(ROUND_TEAMS[: 2 ** (4 - number)]):
        for game in range(1, 5):
            game_id = f"0042400{number}{series}{game}"
            fields = {
                "season_type": PLAYOFFS,
                "game_date": f"2025-06-0{game}",
                "game_id": f"verified-{series}-{game}" if explicit else game_id,
            }
            if explicit:
                fields["playoff_round"] = PLAYOFF_ROUND_LABELS[
                    PLAYOFF_ROUNDS[number - 1]
                ]
            rows.extend(
                [
                    appearance(
                        10 + series,
                        "Round Winner",
                        winner,
                        opponent_abbr=loser,
                        game_result="W",
                        **fields,
                    ),
                    appearance(
                        30 + series,
                        "Round Opponent",
                        loser,
                        opponent_abbr=winner,
                        wl="L",
                        **fields,
                    ),
                ]
            )
    return Evidence(
        rows,
        frozenset({(SEASON, REGULAR), (SEASON, PLAYOFFS)}),
        "fixtures/player-games",
        "p4b-fixture",
        {(SEASON, REGULAR): "2025-01-01", (SEASON, PLAYOFFS): "2025-06-08"},
        complete=True,
        cohort_complete=True,
    )


@pytest.mark.parametrize("number", [1, 2, 3, 4])
@pytest.mark.parametrize("explicit", [True, False])
def test_completed_round_derives_only_verified_nonparticipant(number, explicit):
    evidence = completed_evidence(number, explicit=explicit)
    [record] = derive_nonparticipation_records(evidence)
    event = PLAYOFF_ROUND_LABELS[PLAYOFF_ROUNDS[number - 1]]
    assert record["event"] == event
    assert record["player_id"] == 1
    assert record["participated"] is False
    assert record["is_verified_complete_cohort"] is True
    assert record["season_teams"] == ["NYK"]
    assert len(record["round_game_ids"]) == 4 * 2 ** (4 - number)
    answer = answer_known_nonparticipation(
        f"Did Finch play in the {SEASON} {event}?", SEASON, evidence=evidence
    )
    assert (
        answer["participation_evidence"]["contract_version"] == "event_participation/2"
    )
    assert answer["participation_evidence"]["snapshot_id"] == evidence.snapshot_id
    assert answer["status"] == "not_applicable"
    assert answer["tables"] == answer["charts"] == []
    assert answer["semantic_evidence"] is None


@pytest.mark.parametrize("case", ["partial", "incomplete", "no_round", "open_series"])
def test_evidence_fails_closed(case):
    evidence = completed_evidence()
    if case == "partial":
        evidence = replace(evidence, cohort_complete=False)
    elif case == "incomplete":
        evidence = replace(evidence, complete=False)
    elif case == "no_round":
        evidence = replace(
            evidence,
            rows=[
                {**row, "game_id": "unmapped-" + row["game_id"]}
                if row["season_type"] == PLAYOFFS
                else row
                for row in evidence.rows
            ],
        )
    else:
        evidence = replace(
            evidence, rows=[r for r in evidence.rows if r["game_id"] != "0042400404"]
        )
    assert derive_nonparticipation_records(evidence) == []
    assert (
        answer_known_nonparticipation(
            f"Avery Finch points in the {SEASON} Finals", SEASON, evidence=evidence
        )
        is None
    )


def test_unseen_round_and_uncovered_players_remain_unknown():
    evidence = completed_evidence()
    for question in (
        f"Avery Finch points in the {SEASON} Conference Finals",
        f"Unknown Player points in the {SEASON} NBA Finals",
        f"Blake Reed points in the {SEASON} NBA Finals",
        f"Avery Finch points in the {SEASON} NBA Finals at home",
        f"Compare Avery Finch and Blake Reed in the {SEASON} NBA Finals",
        f"Round Winner points in the {SEASON} NBA Finals",
    ):
        assert (
            answer_known_nonparticipation(question, SEASON, evidence=evidence) is None
        )


def test_observed_round_game_blocks_nonparticipation_even_with_wrong_phase():
    evidence = completed_evidence()
    appearance_in_round_game = {**evidence.rows[0], "game_id": "0042400401"}
    assert (
        derive_nonparticipation_records(
            replace(evidence, rows=[*evidence.rows, appearance_in_round_game])
        )
        == []
    )


def test_earlier_playoff_appearance_qualifies_but_any_round_team_blocks_absence():
    evidence = completed_evidence()
    [focal, *rows] = evidence.rows
    earlier = {
        **focal,
        "season_type": PLAYOFFS,
        "game_id": "0042400301",
        "game_date": "2025-05-24",
    }
    evidence = replace(evidence, rows=[earlier, *rows])
    assert [r["player_id"] for r in derive_nonparticipation_records(evidence)] == [1]

    second_team = {**focal, "team_abbr": "BOS"}
    assert not derive_nonparticipation_records(
        replace(evidence, rows=[*evidence.rows, second_team])
    )


@pytest.mark.parametrize("declares_regular_season", [False, True])
def test_playoff_only_evidence_cannot_verify_season_team_history(
    declares_regular_season,
):
    evidence = completed_evidence()
    earlier_appearance = appearance(
        1,
        "Avery Finch",
        "NYK",
        season_type=PLAYOFFS,
        game_id="0042400301",
        game_date="2025-05-24",
    )
    evidence = replace(
        evidence,
        rows=[earlier_appearance, *evidence.rows[2:]],
        covered_scopes=(
            evidence.covered_scopes
            if declares_regular_season
            else frozenset({(SEASON, PLAYOFFS)})
        ),
    )
    assert not derive_nonparticipation_records(evidence)


def test_losing_team_opponent_labels_do_not_substitute_for_player_rows():
    evidence = completed_evidence()
    evidence = replace(
        evidence,
        rows=[
            row
            for row in evidence.rows
            if (row["game_id"], row["team_abbr"]) != ("0042400404", "BOS")
        ],
    )
    assert not derive_nonparticipation_records(evidence)


@pytest.mark.parametrize("clinching_date", ["2025-06-04", "2025-06-06"])
def test_series_must_end_on_the_clinching_win(clinching_date):
    evidence = completed_evidence()
    extra_game = [
        {
            **row,
            "game_date": "2025-06-05",
            "game_id": "0042400405",
            result_field: result,
        }
        for row, result_field, result in zip(
            evidence.rows[-2:], ["game_result", "wl"], ["L", "W"], strict=True
        )
    ]
    rows = [
        {**row, "game_date": clinching_date} if row["game_id"] == "0042400404" else row
        for row in evidence.rows
    ]
    records = derive_nonparticipation_records(
        replace(evidence, rows=[*rows, *extra_game])
    )
    assert [record["player_id"] for record in records] == (
        [1] if clinching_date == "2025-06-06" else []
    )


def test_curated_v1_reason_and_provenance_override_precomputed_record():
    [catalog] = load_participation_catalog()
    candidate = {
        **catalog,
        "reason": "Derived team did not reach the round.",
        "source_url": "https://example.com",
        "is_verified_complete_cohort": True,
    }
    response = answer_known_nonparticipation(
        "Stephen Curry points in the 2025-26 NBA Finals",
        "2025-26",
        programmatic_records=[candidate],
    )
    assert response["participation_evidence"] == {
        **catalog,
        "contract_version": "event_participation/1",
    }
    response = answer_known_nonparticipation(
        "Stephen Curry total points in the 2025-26 NBA Finals",
        "2025-26",
        programmatic_records=[candidate],
    )
    assert response["participation_evidence"]["reason"] == catalog["reason"]


def test_precomputed_gold_record_requires_attestation_and_no_conflicting_row():
    [record] = derive_nonparticipation_records(completed_evidence())
    record["event_name"] = record.pop("event")
    question = f"Did Avery Finch play in the {SEASON} NBA Finals?"
    assert (
        answer_known_nonparticipation(question, SEASON, programmatic_records=[record])[
            "status"
        ]
        == "not_applicable"
    )
    for records in [
        [{**record, "is_verified_complete_cohort": False}],
        [record, {**record, "participated": True}],
    ]:
        assert (
            answer_known_nonparticipation(
                question, SEASON, programmatic_records=records
            )
            is None
        )


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("source", ""),
        ("snapshot_id", " "),
        ("round_game_ids", []),
        ("round_game_ids", [""]),
    ],
)
def test_precomputed_gold_record_requires_nonempty_provenance(field, invalid_value):
    [record] = derive_nonparticipation_records(completed_evidence())
    question = f"Did Avery Finch play in the {SEASON} NBA Finals?"
    record.pop(field)
    assert (
        answer_known_nonparticipation(question, SEASON, programmatic_records=[record])
        is None
    )
    assert (
        answer_known_nonparticipation(
            question, SEASON, programmatic_records=[{**record, field: invalid_value}]
        )
        is None
    )


@pytest.mark.parametrize("transport", ["", "/stream"])
def test_ask_returns_nonparticipation_before_planning(monkeypatch, transport):
    from app import main

    evidence = completed_evidence()
    agent, model, warehouse = ask_fixture(evidence.rows)
    config = replace(agent.settings, performance_cache_prewarm_enabled=False)
    repo = BigQueryWarehouseRepository(config, client=SimpleNamespace())
    repo._governed_warehouse = warehouse
    for dependency, replacement in (
        (main.get_settings, lambda: config),
        (main.get_repository, lambda: repo),
        (main.get_agent_client, lambda: model),
    ):
        monkeypatch.setitem(main.app.dependency_overrides, dependency, replacement)
    with TestClient(main.app) as client:
        result = ask(
            client,
            f"How many points did Finch score in the {SEASON} NBA Finals?",
            transport,
        )
    assert result["status"] == "not_applicable", result["answer"]
    assert result["ask_route"]["route"] == "event_participation"
    assert result["claim_validation"]["errors"] == []
    assert result["tables"] == result["charts"] == []
    assert not model.calls
    assert warehouse.calls == [[SEASON]]


def test_dbt_event_model_and_singular_integrity_test():
    root = Path(__file__).resolve().parents[1]
    model = (root / "dbt/models/gold/fct_player_event_participation.sql").read_text()
    test = (root / "dbt/tests/player_event_participation_valid.sql").read_text()
    assert "materialized='table'" in model
    assert "env_var('BQ_DATASET_GOLD', env_var('BQ_DATASET', 'nba_gold'))" in model
    assert "ref('dim_game')" in model and "ref('fct_player_game_stats')" in model
    assert "true as participated" in model and "false as participated" in model
    assert "completed_rounds" in model and "is_verified_complete_cohort" in model
    assert "p.round_number = c.round_number" in model
    assert "ref('fct_player_event_participation')" in test
    assert "having count(*) > 1" in test
    assert "where not p.participated" in test and "a.player_id is not null" in test


@pytest.mark.parametrize(
    ("case", "verified"),
    [
        ("no_playoff_player_rows", False),
        ("one_sided_game", False),
        ("game_after_clinch", False),
        ("valid_five_game_series", True),
    ],
)
def test_dbt_event_model_requires_coverage_and_a_chronological_clinch(case, verified):
    root = Path(__file__).resolve().parents[1] / "dbt"
    event = PLAYOFF_ROUND_LABELS[PLAYOFF_ROUNDS[-1]]
    env = Environment()
    env.globals.update(
        config=lambda **_: "",
        env_var=lambda _, default: default,
        ref=lambda name: name,
        playoff_round_name=lambda number: f"case {number} when 4 then '{event}' end",
    )
    model = env.from_string(
        (root / "models/gold/fct_player_event_participation.sql").read_text()
    ).render()
    integrity_test = env.from_string(
        (root / "tests/player_event_participation_valid.sql").read_text()
    ).render()
    with duckdb.connect(config={"threads": 1}) as db:
        db.execute("""
            create table dim_game (
                season varchar, season_type varchar, game_id varchar, game_date date,
                playoff_round_number integer, playoff_round_name varchar,
                home_team_abbr varchar, away_team_abbr varchar, winning_team_abbr varchar
            )
        """)
        db.execute("""
            create table fct_player_game_stats (
                season varchar, player_id integer, player_name varchar, team_abbr varchar,
                season_type varchar, game_id varchar, game_date date
            )
        """)
        db.execute(
            "insert into fct_player_game_stats values (?, 1, 'Avery Finch', 'NYK', ?, 'reg', '2025-04-01')",
            [SEASON, REGULAR],
        )
        games = [(f"game-{n}", f"2025-06-0{n}", "ATL") for n in range(1, 5)]
        if case in {"game_after_clinch", "valid_five_game_series"}:
            if verified:
                games[-1] = ("game-4", "2025-06-06", "ATL")
            games.append(("game-5", "2025-06-05", "BOS"))
        for game_id, game_date, winner in games:
            db.execute(
                "insert into dim_game values (?, ?, ?, ?, 4, ?, 'ATL', 'BOS', ?)",
                [SEASON, PLAYOFFS, game_id, game_date, event, winner],
            )
            if case != "no_playoff_player_rows":
                db.execute(
                    "insert into fct_player_game_stats values (?, 10, 'Round Winner', 'ATL', ?, ?, ?)",
                    [SEASON, PLAYOFFS, game_id, game_date],
                )
                if (case, game_id) != ("one_sided_game", "game-4"):
                    db.execute(
                        "insert into fct_player_game_stats values (?, 30, 'Round Opponent', 'BOS', ?, ?, ?)",
                        [SEASON, PLAYOFFS, game_id, game_date],
                    )

        db.execute("create table fct_player_event_participation as " + model)
        facts = db.execute("""
            select player_id, participated, is_verified_complete_cohort
            from fct_player_event_participation
            order by player_id
        """).fetchall()
        if verified:
            assert facts == [(1, False, True), (10, True, True), (30, True, True)]
        elif case == "no_playoff_player_rows":
            assert facts == []
        else:
            assert facts == [(10, True, False), (30, True, False)]
        assert db.execute(integrity_test).fetchall() == []

        if case in {"no_playoff_player_rows", "one_sided_game"}:
            db.execute(
                """
                insert into fct_player_event_participation
                values (?, ?, 1, 'Avery Finch', false, 'Fixture invalid fact.', true)
            """,
                [SEASON, event],
            )
            assert db.execute(integrity_test).fetchall() == [(SEASON, event, 1)]
