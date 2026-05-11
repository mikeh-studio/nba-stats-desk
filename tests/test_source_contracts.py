from __future__ import annotations

import sys
from pathlib import Path
import json

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dags"))

import nba_source_contracts as contracts


def _game_log_row(**overrides):
    row = {
        "GAME_ID": "0022500001",
        "GAME_DATE": "2026-01-10",
        "MATCHUP": "LAL vs. BOS",
        "WL": "W",
        "MIN": 32.0,
        "FGM": 8.0,
        "FGA": 15.0,
        "FG_PCT": 0.533,
        "FG3M": 2.0,
        "FG3A": 5.0,
        "FG3_PCT": 0.4,
        "FTM": 4.0,
        "FTA": 5.0,
        "FT_PCT": 0.8,
        "OREB": 1.0,
        "DREB": 6.0,
        "PTS": 22,
        "REB": 7,
        "AST": 5,
        "STL": 1,
        "BLK": 0,
        "TOV": 2,
        "PF": 3,
        "PLUS_MINUS": 8.0,
        "SEASON": "2025-26",
        "INGESTED_AT_UTC": "2026-01-10T12:00:00Z",
        "PLAYER_ID": 2544,
        "PLAYER_NAME": "LeBron James",
    }
    row.update(overrides)
    return row


def _player_reference_row(**overrides):
    row = {
        "PLAYER_ID": 2544,
        "FIRST_NAME": "LeBron",
        "LAST_NAME": "James",
        "PLAYER_NAME": "LeBron James",
        "PLAYER_SLUG": "lebron-james",
        "BIRTHDATE": "1984-12-30",
        "SCHOOL": "",
        "COUNTRY": "USA",
        "LAST_AFFILIATION": "",
        "HEIGHT": "6-9",
        "WEIGHT": 250,
        "SEASON_EXP": 22,
        "JERSEY": "23",
        "POSITION": "Forward",
        "ROSTER_STATUS": True,
        "TEAM_ID": 1610612747,
        "TEAM_NAME": "Lakers",
        "TEAM_ABBR": "LAL",
        "TEAM_CODE": "lakers",
        "TEAM_CITY": "Los Angeles",
        "FROM_YEAR": 2003,
        "TO_YEAR": 2026,
        "DRAFT_YEAR": "2003",
        "DRAFT_ROUND": "1",
        "DRAFT_NUMBER": "1",
        "INGESTED_AT_UTC": "2026-01-10T12:00:00Z",
    }
    row.update(overrides)
    return row


def _line_score_row(**overrides):
    row = {
        "GAME_DATE": "2026-01-10",
        "GAME_ID": "0022500001",
        "SEASON": "2025-26",
        "TEAM_ID": 1610612747,
        "TEAM_ABBR": "LAL",
        "TEAM_CITY_NAME": "Los Angeles",
        "TEAM_NICKNAME": "Lakers",
        "TEAM_WINS_LOSSES": "10-4",
        "PTS_QTR1": 30,
        "PTS_QTR2": 25,
        "PTS_QTR3": 28,
        "PTS_QTR4": 27,
        "PTS_OT1": 0,
        "PTS_OT2": 0,
        "PTS_OT3": 0,
        "PTS_OT4": 0,
        "PTS_OT5": 0,
        "PTS_OT6": 0,
        "PTS_OT7": 0,
        "PTS_OT8": 0,
        "PTS_OT9": 0,
        "PTS_OT10": 0,
        "PTS": 110,
        "INGESTED_AT_UTC": "2026-01-10T12:00:00Z",
    }
    row.update(overrides)
    return row


def _schedule_rows():
    return [
        {
            "SCHEDULE_DATE": "2026-01-10",
            "GAME_ID": "0022500001",
            "SEASON": "2025-26",
            "TEAM_ABBR": "LAL",
            "OPPONENT_ABBR": "BOS",
            "HOME_AWAY": "HOME",
            "IS_BACK_TO_BACK": False,
            "GAME_STATUS": "7:30 pm ET",
            "SOURCE_UPDATED_AT_UTC": "2026-01-10T12:00:00Z",
            "INGESTED_AT_UTC": "2026-01-10T12:00:00Z",
        },
        {
            "SCHEDULE_DATE": "2026-01-10",
            "GAME_ID": "0022500001",
            "SEASON": "2025-26",
            "TEAM_ABBR": "BOS",
            "OPPONENT_ABBR": "LAL",
            "HOME_AWAY": "AWAY",
            "IS_BACK_TO_BACK": False,
            "GAME_STATUS": "7:30 pm ET",
            "SOURCE_UPDATED_AT_UTC": "2026-01-10T12:00:00Z",
            "INGESTED_AT_UTC": "2026-01-10T12:00:00Z",
        },
    ]


def _injury_report_row(**overrides):
    row = {
        "REPORT_DATE": "2026-01-10",
        "REPORT_TIME_ET": "05:00 PM",
        "REPORT_TIMESTAMP_UTC": "2026-01-10T22:00:00Z",
        "GAME_DATE": "2026-01-11",
        "GAME_TIME_ET": "07:30 PM",
        "MATCHUP": "LAL vs. BOS",
        "SEASON": "2025-26",
        "TEAM_ABBR": "LAL",
        "TEAM_NAME": "Los Angeles Lakers",
        "PLAYER_ID": 2544,
        "PLAYER_NAME": "LeBron James",
        "PLAYER_NAME_SOURCE": "LeBron James",
        "INJURY_STATUS": "Probable",
        "REASON": "Left ankle soreness",
        "SOURCE_URL": "https://ak-static.cms.nba.com/referee/injury/Injury-Report.pdf",
        "SOURCE_SYSTEM": "nba_official_injury_report",
        "INGESTED_AT_UTC": "2026-01-10T22:05:00Z",
    }
    row.update(overrides)
    return row


def test_contract_files_load():
    parsed = contracts.validate_contract_files()

    assert {contract["domain"] for contract in parsed} == {
        "game_logs",
        "game_line_scores",
        "player_reference",
        "schedule",
        "injury_reports",
    }


def test_valid_game_log_frame_passes():
    frame = pd.DataFrame([_game_log_row()])

    validation = contracts.validate_source_contract("game_logs", frame)

    assert validation.result["status"] == "passed"
    assert validation.result["rows_checked"] == 1
    assert validation.result["rows_quarantined"] == 0
    assert len(validation.frame) == 1


def test_missing_required_column_is_fatal():
    frame = pd.DataFrame([_game_log_row()]).drop(columns=["GAME_DATE"])

    with pytest.raises(contracts.SourceContractError) as exc:
        contracts.validate_source_contract("game_logs", frame)

    assert exc.value.result["status"] == "fatal"
    assert exc.value.result["fatal_count"] == 1
    assert "GAME_DATE" in exc.value.result["violations"][0]["message"]


def test_duplicate_business_key_is_fatal():
    frame = pd.DataFrame(
        [
            _game_log_row(GAME_ID="0022500001"),
            _game_log_row(GAME_ID="0022500002"),
        ]
    )

    with pytest.raises(contracts.SourceContractError) as exc:
        contracts.validate_source_contract("game_logs", frame)

    assert exc.value.result["status"] == "fatal"
    assert any(
        violation["rule"] == "business_key_unique"
        for violation in exc.value.result["violations"]
    )


def test_blank_business_key_is_fatal():
    frame = pd.DataFrame([_game_log_row(GAME_ID="", MATCHUP=" ")])

    with pytest.raises(contracts.SourceContractError) as exc:
        contracts.validate_source_contract("game_logs", frame)

    assert exc.value.result["status"] == "fatal"
    assert any(
        violation["rule"] == "business_key_not_null"
        for violation in exc.value.result["violations"]
    )


def test_quarantine_rule_removes_bad_rows():
    frame = pd.DataFrame(
        [
            _game_log_row(PLAYER_ID=1, MATCHUP="LAL vs. BOS", PTS=18),
            _game_log_row(PLAYER_ID=2, MATCHUP="NYK @ MIA", PTS=120),
        ]
    )

    validation = contracts.validate_source_contract("game_logs", frame)

    assert validation.result["status"] == "quarantine"
    assert validation.result["rows_quarantined"] == 1
    assert validation.frame["PLAYER_ID"].tolist() == [1]
    assert validation.quarantine_frame["PLAYER_ID"].tolist() == [2]
    assert validation.quarantine_frame["_source_row_index"].tolist() == [1]


def test_fatal_contract_error_preserves_quarantined_rows():
    frame = pd.DataFrame([_game_log_row(PLAYER_ID=2, MATCHUP="NYK @ MIA", PTS=120)])

    with pytest.raises(contracts.SourceContractError) as exc:
        contracts.validate_source_contract("game_logs", frame)

    assert exc.value.result["status"] == "fatal"
    assert exc.value.result["rows_quarantined"] == 1
    assert exc.value.quarantine_frame["PLAYER_ID"].tolist() == [2]
    assert any(
        violation["rule"] == "quarantine_exhausted_frame"
        for violation in exc.value.result["violations"]
    )


def test_warning_rule_does_not_drop_rows():
    frame = pd.DataFrame([_player_reference_row(TEAM_ABBR="XYZ")])

    validation = contracts.validate_source_contract("player_reference", frame)

    assert validation.result["status"] == "warning"
    assert validation.result["warning_count"] == 1
    assert validation.result["rows_quarantined"] == 0
    assert len(validation.frame) == 1


def test_valid_schedule_frame_passes_and_result_is_json_serializable():
    frame = pd.DataFrame(_schedule_rows())

    validation = contracts.validate_source_contract("schedule", frame)

    assert validation.result["status"] == "passed"
    assert len(validation.frame) == 2
    json.dumps(validation.result)


def test_injury_report_blank_player_name_source_is_fatal():
    frame = pd.DataFrame([_injury_report_row(PLAYER_NAME_SOURCE=" ")])

    with pytest.raises(contracts.SourceContractError) as exc:
        contracts.validate_source_contract("injury_reports", frame)

    assert exc.value.result["status"] == "fatal"
    assert any(
        violation["rule"] == "business_key_not_null"
        for violation in exc.value.result["violations"]
    )


def test_post_quarantine_fatal_rules_prevent_partial_game_groups():
    frame = pd.DataFrame(
        [
            _line_score_row(),
            _line_score_row(
                TEAM_ID=1610612738,
                TEAM_ABBR="BOS",
                TEAM_CITY_NAME="Boston",
                TEAM_NICKNAME="Celtics",
                PTS=999,
            ),
        ]
    )

    with pytest.raises(contracts.SourceContractError) as exc:
        contracts.validate_source_contract("game_line_scores", frame)

    assert exc.value.result["status"] == "fatal"
    assert any(
        violation["rule"] == "post_quarantine_one_home_and_away_team_per_game"
        for violation in exc.value.result["violations"]
    )
