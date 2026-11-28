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


def completed_evidence(number=4, *, explicit=False, with_participants=True):
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
                fields["playoff_series_game_number"] = game
            for slot in range(5):
                rows.extend(
                    [
                        appearance(
                            100 + series * 10 + slot,
                            f"Round Winner {series}-{slot}",
                            winner,
                            opponent_abbr=loser,
                            game_result="W",
                            **fields,
                        ),
                        appearance(
                            300 + series * 10 + slot,
                            f"Round Opponent {series}-{slot}",
                            loser,
                            opponent_abbr=winner,
                            wl="L",
                            **fields,
                        ),
                    ]
                )
    participants = (
        [
            {
                "season": r["season"],
                "game_id": r["game_id"],
                "player_id": r["player_id"],
                "team_abbr": r["team_abbr"],
                "played": True,
            }
            for r in rows
            if r["season_type"] == PLAYOFFS
        ]
        if with_participants
        else ()
    )
    return Evidence(
        rows,
        frozenset({(SEASON, REGULAR), (SEASON, PLAYOFFS)}),
        "fixtures/player-games",
        "p4b-fixture",
        {(SEASON, REGULAR): "2025-01-01", (SEASON, PLAYOFFS): "2025-06-08"},
        complete=True,
        cohort_complete=True,
        game_participants=participants,
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
        f"Avery Finch PPG in the West in the {SEASON} NBA Finals",
        f"Compare Avery Finch and Blake Reed in the {SEASON} NBA Finals",
        f"Round Winner 0-0 points in the {SEASON} NBA Finals",
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


@pytest.mark.parametrize("case", ["after_clinch", "out_of_order", "same_day", "valid"])
def test_series_must_end_on_the_clinching_win(case):
    evidence = completed_evidence()
    last_game_rows = [row for row in evidence.rows if row["game_id"] == "0042400404"]
    extra_game = [
        {
            **{k: v for k, v in row.items() if k != "game_result"},
            "game_date": "2025-06-05",
            "game_id": "0042400405",
            "wl": (
                "W"
                if (row.get("game_result") == "W") == (case in {"same_day", "valid"})
                else "L"
            ),
        }
        for row in last_game_rows
    ]
    extra_participants = [
        {
            "season": row["season"],
            "game_id": row["game_id"],
            "player_id": row["player_id"],
            "team_abbr": row["team_abbr"],
            "played": True,
        }
        for row in extra_game
    ]
    fourth_game_date = {
        "out_of_order": "2025-06-06",
        "same_day": "2025-06-05",
    }.get(case, "2025-06-04")
    rows = []
    for row in evidence.rows:
        if row["game_id"] == "0042400404":
            row = {**row, "game_date": fourth_game_date}
            if case in {"same_day", "valid"}:
                result = row.pop("game_result", row.get("wl"))
                row["wl"] = "L" if result == "W" else "W"
        rows.append(row)
    records = derive_nonparticipation_records(
        replace(
            evidence,
            rows=[*rows, *extra_game],
            game_participants=[*evidence.game_participants, *extra_participants],
        )
    )
    assert [record["player_id"] for record in records] == (
        [1] if case == "valid" else []
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


def _ask_participation_fixture(evidence):
    agent, model, warehouse = ask_fixture(evidence.rows)
    base_load = warehouse.load

    def load_with_participants(seasons):
        snapshot, loaded = base_load(seasons)
        return snapshot, replace(loaded, game_participants=evidence.game_participants)

    warehouse.load = load_with_participants
    return agent, model, warehouse


@pytest.mark.parametrize("transport", ["", "/stream"])
def test_ask_returns_nonparticipation_before_planning(monkeypatch, transport):
    from app import main

    evidence = completed_evidence()
    agent, model, warehouse = _ask_participation_fixture(evidence)
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


def _render_dbt_participation_sql(
    game_participants_relation: str | None = "game_participants",
) -> tuple[str, str]:
    root = Path(__file__).resolve().parents[1] / "dbt"
    round_cases = " ".join(
        f"when {index} then '{PLAYOFF_ROUND_LABELS[key]}'"
        for index, key in enumerate(PLAYOFF_ROUNDS, start=1)
    )
    env = Environment()
    env.globals.update(
        config=lambda **_: "",
        env_var=lambda _, default: default,
        ref=lambda name: name,
        var=lambda name, default=None: (
            game_participants_relation
            if name == "game_participants_relation"
            else default
        ),
        playoff_round_name=lambda number: f"case {number} {round_cases} end",
    )
    model = env.from_string(
        (root / "models/gold/fct_player_event_participation.sql").read_text()
    ).render()
    integrity_test = env.from_string(
        (root / "tests/player_event_participation_valid.sql").read_text()
    ).render()
    return model, integrity_test


def _create_dbt_participation_tables(db: duckdb.DuckDBPyConnection) -> None:
    db.execute("""
        create table dim_game (
            season varchar, season_type varchar, game_id varchar, game_date date,
            playoff_round_number integer, playoff_round_name varchar,
            playoff_series_game_number integer,
            home_team_abbr varchar, away_team_abbr varchar, winning_team_abbr varchar
        )
    """)
    db.execute("""
        create table fct_player_game_stats (
            season varchar, player_id integer, player_name varchar, team_abbr varchar,
            season_type varchar, game_id varchar, game_date date
        )
    """)
    db.execute("""
        create table game_participants (
            season varchar, game_id varchar, player_id integer,
            team_abbr varchar, played boolean
        )
    """)


def _insert_dbt_game_participants_from_stats(
    db: duckdb.DuckDBPyConnection, *, season: str = SEASON
) -> None:
    db.execute(
        """
        insert into game_participants
        select distinct season, game_id, player_id, team_abbr, true
        from fct_player_game_stats
        where season = ? and season_type = ?
        """,
        [season, PLAYOFFS],
    )


@pytest.mark.parametrize(
    ("case", "verified"),
    [
        ("no_playoff_player_rows", False),
        ("incomplete_team_players", False),
        ("one_sided_game", False),
        ("missing_game_two", False),
        ("game_after_clinch", False),
        ("out_of_order_game", False),
        ("same_day_games", False),
        ("valid_five_game_series", True),
    ],
)
def test_dbt_event_model_requires_coverage_and_a_chronological_clinch(case, verified):
    event = PLAYOFF_ROUND_LABELS[PLAYOFF_ROUNDS[-1]]
    model, integrity_test = _render_dbt_participation_sql()
    with duckdb.connect(config={"threads": 1}) as db:
        _create_dbt_participation_tables(db)
        db.execute(
            "insert into fct_player_game_stats values (?, 1, 'Avery Finch', 'NYK', ?, 'reg', '2025-04-01')",
            [SEASON, REGULAR],
        )
        if case == "missing_game_two":
            games = [
                ("0042400401", "2025-06-01", 1, "ATL"),
                ("0042400403", "2025-06-05", 3, "ATL"),
                ("0042400404", "2025-06-07", 4, "ATL"),
                ("0042400405", "2025-06-09", 5, "ATL"),
            ]
        else:
            games = [(f"004240040{n}", f"2025-06-0{n}", n, "ATL") for n in range(1, 5)]
            if case in {"same_day_games", "valid_five_game_series"}:
                fourth_date = "2025-06-05" if case == "same_day_games" else "2025-06-04"
                games[-1] = ("0042400404", fourth_date, 4, "BOS")
                games.append(("0042400405", "2025-06-05", 5, "ATL"))
            elif case in {"game_after_clinch", "out_of_order_game"}:
                fourth_date = {
                    "game_after_clinch": "2025-06-04",
                    "out_of_order_game": "2025-06-06",
                }[case]
                games[-1] = ("0042400404", fourth_date, 4, "ATL")
                games.append(("0042400405", "2025-06-05", 5, "BOS"))
        for game_id, game_date, game_num, winner in games:
            db.execute(
                "insert into dim_game values (?, ?, ?, ?, 4, ?, ?, 'ATL', 'BOS', ?)",
                [SEASON, PLAYOFFS, game_id, game_date, event, game_num, winner],
            )
            if case != "no_playoff_player_rows":
                for slot in range(5):
                    db.execute(
                        "insert into fct_player_game_stats values (?, ?, ?, 'ATL', ?, ?, ?)",
                        [
                            SEASON,
                            100 + slot,
                            f"Round Winner {slot}",
                            PLAYOFFS,
                            game_id,
                            game_date,
                        ],
                    )
                opponent_count = (
                    4
                    if (case, game_id) == ("incomplete_team_players", "0042400404")
                    else 0
                    if (case, game_id) == ("one_sided_game", "0042400404")
                    else 5
                )
                for slot in range(opponent_count):
                    db.execute(
                        "insert into fct_player_game_stats values (?, ?, ?, 'BOS', ?, ?, ?)",
                        [
                            SEASON,
                            300 + slot,
                            f"Round Opponent {slot}",
                            PLAYOFFS,
                            game_id,
                            game_date,
                        ],
                    )
                if (case, game_id) in {
                    ("incomplete_team_players", "0042400404"),
                    ("one_sided_game", "0042400404"),
                }:
                    for slot in range(5):
                        db.execute(
                            "insert into game_participants values (?, ?, ?, 'ATL', true)",
                            [SEASON, game_id, 100 + slot],
                        )
                        db.execute(
                            "insert into game_participants values (?, ?, ?, 'BOS', true)",
                            [SEASON, game_id, 300 + slot],
                        )
                else:
                    for slot in range(5):
                        db.execute(
                            "insert into game_participants values (?, ?, ?, 'ATL', true)",
                            [SEASON, game_id, 100 + slot],
                        )
                    for slot in range(opponent_count):
                        db.execute(
                            "insert into game_participants values (?, ?, ?, 'BOS', true)",
                            [SEASON, game_id, 300 + slot],
                        )

        _insert_dbt_game_participants_from_stats(db)
        db.execute("create table fct_player_event_participation as " + model)
        facts = db.execute("""
            select player_id, participated, is_verified_complete_cohort
            from fct_player_event_participation
            order by player_id
        """).fetchall()
        expected_participants = [
            *((100 + s, True, verified) for s in range(5)),
            *((300 + s, True, verified) for s in range(5)),
        ]
        if verified:
            assert facts == [(1, False, True), *expected_participants]
        elif case == "no_playoff_player_rows":
            assert facts == []
        else:
            assert facts == expected_participants
        assert db.execute(integrity_test).fetchall() == []

        if case in {
            "no_playoff_player_rows",
            "incomplete_team_players",
            "one_sided_game",
            "missing_game_two",
            "out_of_order_game",
            "same_day_games",
        }:
            db.execute(
                """
                insert into fct_player_event_participation
                values (?, ?, 1, 'Avery Finch', false, 'Fixture invalid fact.', true)
            """,
                [SEASON, event],
            )
            assert db.execute(integrity_test).fetchall() == [(SEASON, event, 1)]


def test_dbt_event_model_requires_regular_season_rows_for_nonparticipation():
    conf_finals = PLAYOFF_ROUND_LABELS[PLAYOFF_ROUNDS[2]]
    finals = PLAYOFF_ROUND_LABELS[PLAYOFF_ROUNDS[3]]
    model, integrity_test = _render_dbt_participation_sql()
    with duckdb.connect(config={"threads": 1}) as db:
        _create_dbt_participation_tables(db)
        db.execute(
            "insert into dim_game values (?, ?, '0042400301', '2025-05-24', 3, ?, 1, 'NYK', 'MIA', 'NYK')",
            [SEASON, PLAYOFFS, conf_finals],
        )
        db.execute(
            "insert into fct_player_game_stats values (?, 1, 'Avery Finch', 'NYK', ?, '0042400301', '2025-05-24')",
            [SEASON, PLAYOFFS],
        )
        for n in range(1, 5):
            game_id = f"004240040{n}"
            game_date = f"2025-06-0{n}"
            db.execute(
                "insert into dim_game values (?, ?, ?, ?, 4, ?, ?, 'ATL', 'BOS', 'ATL')",
                [SEASON, PLAYOFFS, game_id, game_date, finals, n],
            )
            for slot in range(5):
                db.execute(
                    "insert into fct_player_game_stats values (?, ?, ?, 'ATL', ?, ?, ?)",
                    [
                        SEASON,
                        100 + slot,
                        f"Round Winner {slot}",
                        PLAYOFFS,
                        game_id,
                        game_date,
                    ],
                )
                db.execute(
                    "insert into fct_player_game_stats values (?, ?, ?, 'BOS', ?, ?, ?)",
                    [
                        SEASON,
                        300 + slot,
                        f"Round Opponent {slot}",
                        PLAYOFFS,
                        game_id,
                        game_date,
                    ],
                )
                db.execute(
                    "insert into game_participants values (?, ?, ?, 'ATL', true)",
                    [SEASON, game_id, 100 + slot],
                )
                db.execute(
                    "insert into game_participants values (?, ?, ?, 'BOS', true)",
                    [SEASON, game_id, 300 + slot],
                )

        db.execute("create table fct_player_event_participation as " + model)
        facts = db.execute("""
            select event_name, player_id, participated, is_verified_complete_cohort
            from fct_player_event_participation
            order by event_name, player_id
        """).fetchall()
        assert facts == [
            (conf_finals, 1, True, False),
            *((finals, 100 + s, True, False) for s in range(5)),
            *((finals, 300 + s, True, False) for s in range(5)),
        ]
        assert (
            db.execute(
                "select * from fct_player_event_participation where not participated"
            ).fetchall()
            == []
        )
        assert db.execute(integrity_test).fetchall() == []

        db.execute(
            """
            insert into fct_player_event_participation
            values (?, ?, 1, 'Avery Finch', false, 'Fixture invalid fact.', true)
        """,
            [SEASON, finals],
        )
        assert db.execute(integrity_test).fetchall() == [(SEASON, finals, 1)]


def test_dbt_event_model_rejects_regular_season_and_wrong_team_finals_appearances():
    finals = PLAYOFF_ROUND_LABELS[PLAYOFF_ROUNDS[3]]
    model, integrity_test = _render_dbt_participation_sql()
    with duckdb.connect(config={"threads": 1}) as db:
        _create_dbt_participation_tables(db)
        db.execute(
            "insert into fct_player_game_stats values (?, 1, 'Avery Finch', 'NYK', ?, 'reg-1', '2025-04-01')",
            [SEASON, REGULAR],
        )
        db.execute(
            "insert into fct_player_game_stats values (?, 2, 'Casey Regular', 'NYK', ?, 'reg-2', '2025-04-01')",
            [SEASON, REGULAR],
        )
        db.execute(
            "insert into fct_player_game_stats values (?, 3, 'Drew Conflicted', 'NYK', ?, 'reg-3', '2025-04-01')",
            [SEASON, REGULAR],
        )
        # Corrupt rows sharing a Finals game_id: regular-season phase (player 2) and wrong team (player 3).
        db.execute(
            "insert into fct_player_game_stats values (?, 2, 'Casey Regular', 'ATL', ?, '0042400401', '2025-06-01')",
            [SEASON, REGULAR],
        )
        db.execute(
            "insert into fct_player_game_stats values (?, 3, 'Drew Conflicted', 'NYK', ?, '0042400401', '2025-06-01')",
            [SEASON, PLAYOFFS],
        )
        for n in range(1, 5):
            game_id = f"004240040{n}"
            game_date = f"2025-06-0{n}"
            db.execute(
                "insert into dim_game values (?, ?, ?, ?, 4, ?, ?, 'ATL', 'BOS', 'ATL')",
                [SEASON, PLAYOFFS, game_id, game_date, finals, n],
            )
            for slot in range(5):
                db.execute(
                    "insert into fct_player_game_stats values (?, ?, ?, 'ATL', ?, ?, ?)",
                    [
                        SEASON,
                        100 + slot,
                        f"Round Winner {slot}",
                        PLAYOFFS,
                        game_id,
                        game_date,
                    ],
                )
                db.execute(
                    "insert into fct_player_game_stats values (?, ?, ?, 'BOS', ?, ?, ?)",
                    [
                        SEASON,
                        300 + slot,
                        f"Round Opponent {slot}",
                        PLAYOFFS,
                        game_id,
                        game_date,
                    ],
                )
                db.execute(
                    "insert into game_participants values (?, ?, ?, 'ATL', true)",
                    [SEASON, game_id, 100 + slot],
                )
                db.execute(
                    "insert into game_participants values (?, ?, ?, 'BOS', true)",
                    [SEASON, game_id, 300 + slot],
                )

        db.execute("create table fct_player_event_participation as " + model)
        facts = db.execute("""
            select player_id, participated, is_verified_complete_cohort
            from fct_player_event_participation
            where player_id in (1, 2, 3)
            order by player_id
        """).fetchall()
        assert facts == [(1, False, True)]
        assert db.execute(integrity_test).fetchall() == []

        db.execute(
            """
            insert into fct_player_event_participation
            values (?, ?, 2, 'Casey Regular', true, 'Invalid positive.', true)
        """,
            [SEASON, finals],
        )
        assert db.execute(integrity_test).fetchall() == [(SEASON, finals, 2)]


def test_missing_playoff_game_with_four_wins_fails_closed():
    evidence = completed_evidence()
    # Games 1, 3, 4, 5 all won by ATL (game 2 missing).
    rows = [r for r in evidence.rows if r["game_id"] != "0042400402"]
    game5_rows = [
        {
            **r,
            "game_id": "0042400405",
            "game_date": "2025-06-05",
        }
        for r in evidence.rows
        if r["game_id"] == "0042400404"
    ]
    gapped = replace(evidence, rows=[*rows, *game5_rows])
    assert derive_nonparticipation_records(gapped) == []
    assert (
        answer_known_nonparticipation(
            f"Did Avery Finch play in the {SEASON} NBA Finals?",
            SEASON,
            evidence=gapped,
        )
        is None
    )


def test_incomplete_team_rows_and_participant_manifest_checks():
    evidence = completed_evidence()
    # Drop one BOS player in game 4 so BOS has only 4 players without a manifest.
    four_player_rows = [
        r
        for r in evidence.rows
        if not (r["game_id"] == "0042400404" and r["player_id"] == 304)
    ]
    assert (
        derive_nonparticipation_records(replace(evidence, rows=four_player_rows)) == []
    )

    # A played=True manifest record contradicting missing box scores must withhold nonparticipation.
    manifest = [
        {
            "game_id": r["game_id"],
            "player_id": r["player_id"],
            "team_abbr": r["team_abbr"],
            "played": True,
        }
        for r in evidence.rows
        if r["season_type"] == PLAYOFFS
    ]
    manifest.append(
        {
            "game_id": "0042400404",
            "player_id": 1,
            "team_abbr": "ATL",
            "played": True,
        }
    )
    contradicted = replace(evidence, game_participants=manifest)
    assert derive_nonparticipation_records(contradicted) == []

    # A compact explicit manifest confirming all played=True rows certifies the round.
    compact_rows = [
        r
        for r in evidence.rows
        if r["season_type"] == REGULAR or r["player_id"] in {100, 300}
    ]
    compact_manifest = [
        {
            "game_id": r["game_id"],
            "player_id": r["player_id"],
            "team_abbr": r["team_abbr"],
            "played": True,
        }
        for r in compact_rows
        if r["season_type"] == PLAYOFFS
    ]
    assert [
        r["player_id"]
        for r in derive_nonparticipation_records(
            replace(evidence, rows=compact_rows, game_participants=compact_manifest)
        )
    ] == [1]


def test_source_policy_allowlist_cannot_be_bypassed_in_participation(monkeypatch):
    from app.agent.espn_dev_source import RetainedReviewSource
    from app.agent.semantic_serving import SemanticWarehouse
    from app.agent.semantic_source import ScopedSemanticSource
    from app.agent.source_policy import SourcePolicy
    from tests.test_scoped_source_policy import RELATION, CapturingClient

    evidence = completed_evidence()
    policy = SourcePolicy(
        relations=((SEASON, RELATION),),
        phases=(REGULAR, PLAYOFFS),
        snapshot_at="2025-06-30T12:00:00+00:00",
        allowed_player_ids=frozenset({2}),
        source_mode="retained_local_review",
    )
    client = CapturingClient(rows=list(evidence.rows))
    source = RetainedReviewSource(
        client,
        project="test-project",
        source_policy=policy,
        max_rows=policy.max_rows,
        maximum_bytes_billed=policy.maximum_bytes_billed,
    )
    warehouse = SemanticWarehouse(ScopedSemanticSource(source, policy.phases))
    agent, model, _ = ask_fixture(evidence.rows)
    agent.semantic_agent.warehouse = warehouse

    # Player 1 is outside allowed_player_ids={2}; must fail identity check, never not_applicable.
    denied = agent.answer(f"How did Avery Finch perform in the {SEASON} NBA Finals?")
    assert denied["status"] == "not_found"

    # When player 1 IS authorized while round teams (100..304) are outside allowlist,
    # full evidence with verified participant coverage verifies the round while turn.players
    # still limits player lookup; without verified participant coverage, it remains unknown.
    allowed_policy = replace(policy, allowed_player_ids=frozenset({1}))
    allowed_source = RetainedReviewSource(
        CapturingClient(rows=list(evidence.rows)),
        project="test-project",
        source_policy=allowed_policy,
        max_rows=allowed_policy.max_rows,
        maximum_bytes_billed=allowed_policy.maximum_bytes_billed,
    )
    allowed_warehouse = SemanticWarehouse(
        ScopedSemanticSource(allowed_source, allowed_policy.phases)
    )
    base_load = allowed_warehouse.load

    def load_with_participants(seasons):
        snapshot, loaded = base_load(seasons)
        return snapshot, replace(loaded, game_participants=evidence.game_participants)

    allowed_warehouse.load = load_with_participants
    agent.semantic_agent.warehouse = allowed_warehouse
    allowed = agent.answer(f"How did Avery Finch perform in the {SEASON} NBA Finals?")
    assert allowed["status"] == "not_applicable"
    assert not model.calls


@pytest.mark.parametrize(
    ("question", "number"),
    [
        (f"What was Avery Finch's PPG in the {SEASON} Conference Finals?", 3),
        (f"What was Avery Finch's RPG in the {SEASON} Conference Semis?", 2),
        (f"Avery Finch APG in the {SEASON} First Round", 1),
        (f"What was Avery Finch's TS% in the {SEASON} NBA Finals?", 4),
        (f"Avery Finch true shooting percentage in the {SEASON} NBA Finals", 4),
    ],
)
def test_supported_round_spans_and_metric_aliases_precede_modifier_checks(
    question, number
):
    evidence = completed_evidence(number)
    response = answer_known_nonparticipation(question, SEASON, evidence=evidence)
    assert response is not None
    assert response["status"] == "not_applicable"


@pytest.mark.parametrize(
    ("question", "number"),
    [
        (f"What was Avery Finch's PPG in the {SEASON} Western Conference Finals?", 3),
        (f"What was Avery Finch's TS% in the {SEASON} East Conference Semis?", 2),
    ],
)
def test_conference_qualified_round_deferred_to_scope_refusal(question, number):
    evidence = completed_evidence(number)
    assert answer_known_nonparticipation(question, SEASON, evidence=evidence) is None


def test_curated_shortcut_uses_warehouse_source_policy_and_rejects_conflicts():
    from app.agent.espn_dev_source import RetainedReviewSource
    from app.agent.semantic_serving import SemanticWarehouse
    from app.agent.semantic_source import ScopedSemanticSource
    from app.agent.semantics import SemanticError
    from app.agent.source_policy import SourcePolicy
    from tests.test_scoped_source_policy import RELATION, CapturingClient

    evidence = completed_evidence()
    rows_2025 = [
        {
            **r,
            "season": "2025-26",
            "game_date": r["game_date"].replace("2025-", "2026-"),
            "game_id": r["game_id"].replace("00424", "00425"),
        }
        for r in evidence.rows
    ]
    policy = SourcePolicy(
        relations=(("2025-26", RELATION),),
        phases=(REGULAR, PLAYOFFS),
        snapshot_at="2026-06-30T12:00:00+00:00",
        allowed_player_ids=frozenset({1}),
        source_mode="retained_local_review",
    )
    source = RetainedReviewSource(
        CapturingClient(rows=rows_2025),
        project="test-project",
        source_policy=policy,
        max_rows=policy.max_rows,
        maximum_bytes_billed=policy.maximum_bytes_billed,
    )
    warehouse = SemanticWarehouse(ScopedSemanticSource(source, policy.phases))
    agent, model, _ = ask_fixture(rows_2025)
    agent.semantic_agent.warehouse = warehouse

    # Curry (201939) is outside the warehouse allowlist even though repo.source_policy is None.
    denied = agent.answer(
        "How many points did Stephen Curry score in the 2025-26 NBA Finals?"
    )
    assert denied["status"] == "not_found"
    assert not model.calls

    # Conflicting repo and warehouse policies must be rejected before curated lookup.
    agent.repo.source_policy = replace(policy, allowed_player_ids=frozenset({201939}))
    with pytest.raises(SemanticError) as exc:
        agent.answer(
            "How many points did Stephen Curry score in the 2025-26 NBA Finals?"
        )
    assert exc.value.code == "source_policy_denied"


def test_source_policy_intersects_explicit_roster_and_checks_season_and_phase():
    from app.agent.source_policy import SourcePolicy
    from tests.test_scoped_source_policy import RELATION

    [catalog] = load_participation_catalog()
    evidence = completed_evidence()
    [record] = derive_nonparticipation_records(evidence)
    curry_roster = [
        {"player_id": catalog["player_id"], "player_name": catalog["player_name"]},
        {"player_id": 1, "player_name": "Avery Finch"},
    ]

    # Explicit roster cannot override policy.allowed_player_ids.
    disallowed_id_policy = SourcePolicy(
        relations=((SEASON, RELATION), ("2025-26", RELATION)),
        phases=(REGULAR, PLAYOFFS),
        snapshot_at="2026-06-30T12:00:00+00:00",
        allowed_player_ids=frozenset({2}),
        source_mode="retained_local_review",
    )
    assert (
        answer_known_nonparticipation(
            "Did Stephen Curry play in the 2025-26 NBA Finals?",
            "2025-26",
            players=curry_roster,
            source_policy=disallowed_id_policy,
        )
        is None
    )
    assert (
        answer_known_nonparticipation(
            f"Did Avery Finch play in the {SEASON} NBA Finals?",
            SEASON,
            players=curry_roster,
            programmatic_records=[record],
            source_policy=disallowed_id_policy,
        )
        is None
    )

    # Disallowed season or disallowed Playoffs phase fails before curated and programmatic lookup.
    wrong_season_policy = replace(
        disallowed_id_policy,
        relations=(("2023-24", RELATION),),
        allowed_player_ids=frozenset({catalog["player_id"], 1}),
    )
    regular_only_policy = replace(
        disallowed_id_policy,
        phases=(REGULAR,),
        allowed_player_ids=frozenset({catalog["player_id"], 1}),
    )
    for bad_policy in (wrong_season_policy, regular_only_policy):
        assert (
            answer_known_nonparticipation(
                "Did Stephen Curry play in the 2025-26 NBA Finals?",
                "2025-26",
                source_policy=bad_policy,
            )
            is None
        )
        assert (
            answer_known_nonparticipation(
                f"Did Avery Finch play in the {SEASON} NBA Finals?",
                SEASON,
                players=curry_roster,
                programmatic_records=[record],
                source_policy=bad_policy,
            )
            is None
        )


def test_capture_retains_series_game_number_for_gap_detection():
    from app.agent.espn_dev_source import RetainedReviewSource
    from app.agent.semantic_source import snapshot_evidence
    from app.agent.source_policy import SourcePolicy
    from tests.test_scoped_source_policy import RELATION, CapturingClient

    evidence = completed_evidence()
    # Authoritative game numbers 1,3,4,5 with ID suffixes 1,2,3,4.
    num_by_id = {
        "0042400401": 1,
        "0042400402": 3,
        "0042400403": 4,
        "0042400404": 5,
    }
    rows = [
        {
            **r,
            "home_away": "HOME" if r["team_abbr"] == "ATL" else "AWAY",
            "wl": r.get("wl") or r.get("game_result"),
            "playoff_round": "NBA Finals" if r["season_type"] == PLAYOFFS else None,
            "playoff_series_game_number": num_by_id.get(r["game_id"]),
        }
        for r in evidence.rows
    ]
    policy = SourcePolicy(
        relations=((SEASON, RELATION),),
        phases=(REGULAR, PLAYOFFS),
        snapshot_at="2025-06-30T12:00:00+00:00",
        allowed_player_ids=frozenset({1}),
        source_mode="retained_local_review",
    )
    client = CapturingClient(rows=rows)
    snapshot = RetainedReviewSource(
        client,
        project="test-project",
        source_policy=policy,
        max_rows=policy.max_rows,
        maximum_bytes_billed=policy.maximum_bytes_billed,
    ).capture_scoped([SEASON], [REGULAR, PLAYOFFS])
    assert "playoff_series_game_number" in snapshot["capture"]["sql"]
    captured_evidence = snapshot_evidence(snapshot)
    assert derive_nonparticipation_records(captured_evidence) == []


def test_unclassified_playoff_game_withholds_round_closure():
    evidence = completed_evidence()
    extra_game = [
        {
            **r,
            "game_id": "unclassified-finals-5",
            "game_date": "2025-06-06",
            "playoff_round": None,
        }
        for r in evidence.rows
        if r["game_id"] == "0042400404"
    ]
    assert (
        derive_nonparticipation_records(
            replace(evidence, rows=[*evidence.rows, *extra_game])
        )
        == []
    )
    # Also when unclassified playoff game is only in game_results.
    assert (
        derive_nonparticipation_records(
            replace(
                evidence,
                game_results=[
                    {
                        "game_id": "unclassified-gr-5",
                        "season": SEASON,
                        "season_type": PLAYOFFS,
                        "game_date": "2025-06-06",
                        "home_team_abbr": "ATL",
                        "away_team_abbr": "BOS",
                        "home_score": 95,
                        "away_score": 102,
                    }
                ],
            )
        )
        == []
    )


def test_contradictory_official_game_results_blocks_clinch():
    evidence = completed_evidence()
    valid_game4 = {
        "game_id": "0042400404",
        "season": SEASON,
        "season_type": PLAYOFFS,
        "game_date": "2025-06-04",
        "home_team_abbr": "ATL",
        "away_team_abbr": "BOS",
        "home_score": 108,
        "away_score": 99,
    }
    assert [
        r["player_id"]
        for r in derive_nonparticipation_records(
            replace(evidence, game_results=[valid_game4])
        )
    ] == [1]

    for bad in (
        {**valid_game4, "home_score": 95, "away_score": 102},
        {**valid_game4, "game_date": "2025-06-05"},
        {**valid_game4, "away_team_abbr": "NYK"},
    ):
        assert (
            derive_nonparticipation_records(replace(evidence, game_results=[bad])) == []
        )


def test_partial_manifest_cannot_bypass_five_player_fallback_and_dnp_blocks():
    evidence = completed_evidence()
    two_per_team_rows = [
        r
        for r in evidence.rows
        if r["season_type"] == REGULAR or r["player_id"] in {100, 101, 300, 301}
    ]
    one_per_team_manifest = [
        {
            "game_id": r["game_id"],
            "player_id": r["player_id"],
            "team_abbr": r["team_abbr"],
            "played": True,
        }
        for r in two_per_team_rows
        if r["season_type"] == PLAYOFFS and r["player_id"] in {100, 300}
    ]
    assert (
        derive_nonparticipation_records(
            replace(
                evidence,
                rows=two_per_team_rows,
                game_participants=one_per_team_manifest,
            )
        )
        == []
    )

    # Full manifest on full rows + DNP for Finch (player 1) on finalist BOS must withhold.
    full_manifest = [
        {
            "game_id": r["game_id"],
            "player_id": r["player_id"],
            "team_abbr": r["team_abbr"],
            "played": True,
        }
        for r in evidence.rows
        if r["season_type"] == PLAYOFFS
    ]
    dnp_finalist = [
        *full_manifest,
        {
            "game_id": "0042400401",
            "player_id": 1,
            "team_abbr": "BOS",
            "played": False,
        },
    ]
    assert (
        derive_nonparticipation_records(
            replace(evidence, game_participants=dnp_finalist)
        )
        == []
    )

    # DNP on non-finalist team (e.g. CHA in First Round / Regular Season) does not block.
    dnp_nonfinalist = [
        *full_manifest,
        {
            "game_id": "0042400101",
            "player_id": 1,
            "team_abbr": "CHA",
            "played": False,
        },
    ]
    assert [
        r["player_id"]
        for r in derive_nonparticipation_records(
            replace(evidence, game_participants=dnp_nonfinalist)
        )
    ] == [1]


@pytest.mark.parametrize(
    "verb", ["participate in", "reach", "make", "appear in", "play in"]
)
def test_unique_surname_with_participation_verbs_through_stats_agent(verb):
    evidence = completed_evidence()
    agent, model, warehouse = _ask_participation_fixture(evidence)
    response = agent.answer(f"Did Finch {verb} the {SEASON} NBA Finals?")
    assert response["status"] == "not_applicable", response["answer"]
    assert "Avery Finch" in response["answer"]
    assert not model.calls
    assert warehouse.calls == [[SEASON]]


def test_dbt_rejects_unclassified_playoff_game_and_false_negative_for_finalist_player():
    event = PLAYOFF_ROUND_LABELS[PLAYOFF_ROUNDS[-1]]
    model, integrity_test = _render_dbt_participation_sql()
    with duckdb.connect(config={"threads": 1}) as db:
        _create_dbt_participation_tables(db)
        db.execute(
            "insert into fct_player_game_stats values (?, 1, 'Avery Finch', 'NYK', ?, 'reg-1', '2025-04-01')",
            [SEASON, REGULAR],
        )
        db.execute(
            "insert into fct_player_game_stats values (?, 2, 'Blake Reed', 'BOS', ?, 'reg-2', '2025-04-01')",
            [SEASON, REGULAR],
        )
        for n in range(1, 5):
            game_id = f"004240040{n}"
            game_date = f"2025-06-0{n}"
            db.execute(
                "insert into dim_game values (?, ?, ?, ?, 4, ?, ?, 'ATL', 'BOS', 'ATL')",
                [SEASON, PLAYOFFS, game_id, game_date, event, n],
            )
            for slot in range(5):
                db.execute(
                    "insert into fct_player_game_stats values (?, ?, ?, 'ATL', ?, ?, ?)",
                    [
                        SEASON,
                        100 + slot,
                        f"Round Winner {slot}",
                        PLAYOFFS,
                        game_id,
                        game_date,
                    ],
                )
                db.execute(
                    "insert into fct_player_game_stats values (?, ?, ?, 'BOS', ?, ?, ?)",
                    [
                        SEASON,
                        300 + slot,
                        f"Round Opponent {slot}",
                        PLAYOFFS,
                        game_id,
                        game_date,
                    ],
                )
                db.execute(
                    "insert into game_participants values (?, ?, ?, 'ATL', true)",
                    [SEASON, game_id, 100 + slot],
                )
                db.execute(
                    "insert into game_participants values (?, ?, ?, 'BOS', true)",
                    [SEASON, game_id, 300 + slot],
                )

        db.execute("create table fct_player_event_participation as " + model)
        # Blake Reed (BOS, no Finals rows) must NOT get a false negative row.
        assert db.execute(
            "select player_id from fct_player_event_participation where not participated"
        ).fetchall() == [(1,)]
        assert db.execute(integrity_test).fetchall() == []

        # Inserting a false negative for Blake Reed (BOS) or a player with no prior rows fails integrity test.
        db.execute(
            "insert into fct_player_event_participation values (?, ?, 2, 'Blake Reed', false, 'False negative.', true)",
            [SEASON, event],
        )
        db.execute(
            "insert into fct_player_event_participation values (?, ?, 999, 'Phantom Player', false, 'No prior.', true)",
            [SEASON, event],
        )
        assert sorted(db.execute(integrity_test).fetchall()) == [
            (SEASON, event, 2),
            (SEASON, event, 999),
        ]

    # An unclassified playoff game for ATL/BOS withholds closure in dbt model too.
    with duckdb.connect(config={"threads": 1}) as db:
        _create_dbt_participation_tables(db)
        db.execute(
            "insert into fct_player_game_stats values (?, 1, 'Avery Finch', 'NYK', ?, 'reg-1', '2025-04-01')",
            [SEASON, REGULAR],
        )
        for n in range(1, 5):
            game_id = f"004240040{n}"
            game_date = f"2025-06-0{n}"
            db.execute(
                "insert into dim_game values (?, ?, ?, ?, 4, ?, ?, 'ATL', 'BOS', 'ATL')",
                [SEASON, PLAYOFFS, game_id, game_date, event, n],
            )
            for slot in range(5):
                db.execute(
                    "insert into fct_player_game_stats values (?, ?, ?, 'ATL', ?, ?, ?)",
                    [
                        SEASON,
                        100 + slot,
                        f"Round Winner {slot}",
                        PLAYOFFS,
                        game_id,
                        game_date,
                    ],
                )
                db.execute(
                    "insert into fct_player_game_stats values (?, ?, ?, 'BOS', ?, ?, ?)",
                    [
                        SEASON,
                        300 + slot,
                        f"Round Opponent {slot}",
                        PLAYOFFS,
                        game_id,
                        game_date,
                    ],
                )
        db.execute(
            "insert into dim_game values (?, ?, '0042400405', '2025-06-06', null, null, 5, 'ATL', 'BOS', 'BOS')",
            [SEASON, PLAYOFFS],
        )
        _insert_dbt_game_participants_from_stats(db)
        db.execute("create table fct_player_event_participation as " + model)
        assert (
            db.execute(
                "select * from fct_player_event_participation where not participated"
            ).fetchall()
            == []
        )
        assert (
            db.execute(
                "select * from fct_player_event_participation where is_verified_complete_cohort"
            ).fetchall()
            == []
        )
        assert db.execute(integrity_test).fetchall() == []


def test_deleting_24pt_finals_row_with_five_per_team_never_certifies_absence():
    event = PLAYOFF_ROUND_LABELS[PLAYOFF_ROUNDS[-1]]
    full_evidence = completed_evidence()
    finch_finals_row = appearance(
        1,
        "Avery Finch",
        "ATL",
        season_type=PLAYOFFS,
        game_date="2025-06-04",
        game_id="0042400404",
        opponent_abbr="BOS",
        game_result="W",
        pts=24,
    )
    full_manifest_with_finch = [
        *full_evidence.game_participants,
        {
            "season": SEASON,
            "game_id": "0042400404",
            "player_id": 1,
            "team_abbr": "ATL",
            "played": True,
        },
    ]
    # Delete Avery Finch's 24-pt Finals box-score row, leaving 5 players per team.
    missing_row_evidence_no_manifest = replace(full_evidence, game_participants=())
    missing_row_evidence_with_manifest = replace(
        full_evidence, game_participants=full_manifest_with_finch
    )
    for ev in (missing_row_evidence_no_manifest, missing_row_evidence_with_manifest):
        assert derive_nonparticipation_records(ev) == []
        assert (
            answer_known_nonparticipation(
                f"Did Avery Finch play in the {SEASON} {event}?",
                SEASON,
                evidence=ev,
            )
            is None
        )
        agent, _, _ = _ask_participation_fixture(ev)
        response = agent.answer(
            f"How many points did Avery Finch score in the {SEASON} {event}?"
        )
        assert response["status"] != "not_applicable"
        assert "did not participate" not in response["answer"].lower()

    # dbt: without game_participants OR with game_participants listing Finch played=True,
    # deleting Finch's 24-pt Finals row (5 players/team remaining) must never certify absence.
    for relation in (None, "game_participants"):
        model, integrity_test = _render_dbt_participation_sql(relation)
        with duckdb.connect(config={"threads": 1}) as db:
            _create_dbt_participation_tables(db)
            db.execute(
                "insert into fct_player_game_stats values (?, 1, 'Avery Finch', 'NYK', ?, 'reg-1', '2025-04-01')",
                [SEASON, REGULAR],
            )
            for n in range(1, 5):
                game_id = f"004240040{n}"
                game_date = f"2025-06-0{n}"
                db.execute(
                    "insert into dim_game values (?, ?, ?, ?, 4, ?, ?, 'ATL', 'BOS', 'ATL')",
                    [SEASON, PLAYOFFS, game_id, game_date, event, n],
                )
                for slot in range(5):
                    db.execute(
                        "insert into fct_player_game_stats values (?, ?, ?, 'ATL', ?, ?, ?)",
                        [
                            SEASON,
                            100 + slot,
                            f"Round Winner {slot}",
                            PLAYOFFS,
                            game_id,
                            game_date,
                        ],
                    )
                    db.execute(
                        "insert into fct_player_game_stats values (?, ?, ?, 'BOS', ?, ?, ?)",
                        [
                            SEASON,
                            300 + slot,
                            f"Round Opponent {slot}",
                            PLAYOFFS,
                            game_id,
                            game_date,
                        ],
                    )
            if relation is not None:
                _insert_dbt_game_participants_from_stats(db)
                db.execute(
                    "insert into game_participants values (?, '0042400404', 1, 'ATL', true)",
                    [SEASON],
                )
            db.execute("create table fct_player_event_participation as " + model)
            assert (
                db.execute(
                    """
                    select * from fct_player_event_participation
                    where not participated or is_verified_complete_cohort
                    """
                ).fetchall()
                == []
            )
            assert (
                db.execute(
                    """
                    select player_id, participated, is_verified_complete_cohort
                    from fct_player_event_participation
                    where player_id = 1
                    """
                ).fetchall()
                == []
            )
            assert db.execute(integrity_test).fetchall() == []
            db.execute(
                "insert into fct_player_event_participation values (?, ?, 1, 'Avery Finch', false, 'False negative.', true)",
                [SEASON, event],
            )
            assert db.execute(integrity_test).fetchall() == [(SEASON, event, 1)]
    assert finch_finals_row["pts"] == 24


def test_repo_source_policy_enforced_across_explicit_semantic_warehouse_and_retries():
    from app.agent.espn_dev_source import RetainedReviewSource
    from app.agent.semantic_serving import SemanticWarehouse
    from app.agent.semantic_source import ScopedSemanticSource
    from app.agent.service import StatsAgent
    from app.agent.source_policy import SourcePolicy
    from tests.test_playoff_rounds_and_positions import Model, settings
    from tests.test_scoped_source_policy import RELATION, CapturingClient

    evidence = completed_evidence()
    repo_policy = SourcePolicy(
        relations=((SEASON, RELATION),),
        phases=(REGULAR, PLAYOFFS),
        snapshot_at="2025-06-30T12:00:00+00:00",
        allowed_player_ids=frozenset({2}),
        source_mode="retained_local_review",
    )
    # Explicitly supplied warehouse whose source has source_policy=None;
    # repo.source_policy (allowing only player 2) must govern evidence loading,
    # roster preparation, and BOTH participation checks.
    source = RetainedReviewSource(
        CapturingClient(rows=list(evidence.rows)),
        project="test-project",
        source_policy=None,
        max_rows=repo_policy.max_rows,
        maximum_bytes_billed=repo_policy.maximum_bytes_billed,
    )
    warehouse = SemanticWarehouse(ScopedSemanticSource(source, repo_policy.phases))
    base_load = warehouse.load

    def load_with_participants(seasons):
        snapshot, loaded = base_load(seasons)
        return snapshot, replace(loaded, game_participants=evidence.game_participants)

    warehouse.load = load_with_participants

    config = replace(settings(SEASON), ask_claim_enforcement="enforce")
    repo = SimpleNamespace(settings=config, source_policy=repo_policy)
    model = Model(season_type=PLAYOFFS, aggregation="total")
    agent = StatsAgent(config, repo, client=model, semantic_warehouse=warehouse)

    for question in (
        f"Did Avery Finch play in the {SEASON} NBA Finals?",
        f"How many points did Avery Finch score in the {SEASON} NBA Finals?",
        f"How did Finch perform in the {SEASON} NBA Finals?",
    ):
        response = agent.answer(question)
        assert response["status"] == "not_found", response
        assert response["status"] != "not_applicable"
        assert "Avery Finch" not in response["answer"]
        assert "NYK" not in response["answer"]
        assert "did not participate" not in response["answer"].lower()
        assert response.get("participation_evidence") is None
        assert not model.calls
