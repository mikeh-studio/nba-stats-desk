import copy
import json

import pytest
from scripts.espn_game_mapping_recovery import (
    DATASET,
    pbp_records,
    plan,
    publication_sql,
)
from scripts.parse_gamebooks import sha


def inputs():
    row = dict(
        source_game_id="1234567",
        season="2025-26",
        game_date="2025-10-22",
        home="NYK",
        away="BOS",
        home_score=100,
        away_score=99,
        result_revision="result-a",
        box_revision="box-a",
        result_status="accepted",
        box_status="accepted",
        nba_game_id=None,
    )
    record = dict(
        GameId="0022500001",
        Date=row["game_date"],
        HomeTeamAbbreviation="NYK",
        AwayTeamAbbreviation="BOS",
        HomePoints=100,
        AwayPoints=99,
    )
    body = b"BOS 99 FINAL 100 NYK LINESCORES TEAM Q1 Q2 Q3 Q4 FINAL BOS 20 20 20 39 99 NYK 25 25 25 25 100"
    meta = dict(
        method="operator_reviewed_cua_dom",
        url="https://www.nba.com/game/bos-vs-nyk-0022500001",
        title="Boston Celtics vs New York Knicks Oct 22, 2025 Game Summary | NBA.com",
        observed_at="2025-10-23T12:00:00+00:00",
        sha256=sha(body),
    )
    return row, record, meta, body


def test_separate_identity_preserves_provider_discrepancy():
    row, record, meta, body = inputs()
    record["AwayPoints"] = 97
    result = plan([row], [record], {row["source_game_id"]: (meta, body)})
    assert result["mappings"][0]["record"]["AwayPoints"] == 99
    assert result["mappings"][0]["record"]["method"] == "reviewed_nba_visible_final_v1"
    assert result["checks"][0]["status"] == "mismatch"
    assert (
        json.loads(result["checks"][0]["detail"])["provider_records"][0]["AwayPoints"]
        == 97
    )
    assert result["checks"][1]["status"] == "verified_identity"


def test_conflict_without_official_evidence_does_not_map():
    row, record, _, _ = inputs()
    record["AwayPoints"] = 97
    result = plan([row], [record], {})
    assert not result["mappings"]
    assert result["checks"][0]["status"] == "mismatch"


def test_matching_pbp_record_is_still_required_to_pass_original_validator():
    row, record, _, _ = inputs()
    assert (
        plan([row], [record], {})["mappings"][0]["record"]["GameId"] == record["GameId"]
    )
    assert not plan([row], [record, record], {})["mappings"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("method", "http_200_only"),
        ("url", "https://example.com/game/bos-vs-nyk-0022500001"),
        ("url", "https://www.nba.com/game/bos-vs-nyk-0022600001"),
        (
            "title",
            "Boston Celtics vs New York Knicks Oct 23, 2025 Game Summary | NBA.com",
        ),
        ("sha256", "bad"),
        ("observed_at", "2025-10-23T12:00:00"),
    ],
)
def test_wrong_primary_evidence_fails(field, value):
    row, _, meta, body = inputs()
    meta[field] = value
    with pytest.raises(ValueError):
        plan([row], [], {row["source_game_id"]: (meta, body)})


def test_unrelated_ticker_score_cannot_validate_missing_linescore():
    row, _, meta, body = inputs()
    body = body.replace(b"LINESCORES", b"OTHER GAMES")
    meta["sha256"] = sha(body)
    with pytest.raises(ValueError):
        plan([row], [], {row["source_game_id"]: (meta, body)})


def test_wrong_final_or_quarter_sum_fails():
    row, _, meta, body = inputs()
    body = body.replace(b"20 20 20 39", b"20 20 20 37")
    meta["sha256"] = sha(body)
    with pytest.raises(ValueError):
        plan([row], [], {row["source_game_id"]: (meta, body)})


def test_existing_identity_conflict_fails():
    row, record, _, _ = inputs()
    row["nba_game_id"] = "0022500002"
    with pytest.raises(ValueError):
        plan([row], [record], {})


def test_stale_box_fails():
    row, record, _, _ = inputs()
    row["box_status"] = "stale"
    with pytest.raises(ValueError):
        plan([row], [record], {})


def test_duplicate_destination_fails_batch():
    row, record, _, _ = inputs()
    second = copy.deepcopy(row)
    second["source_game_id"] = "1234568"
    with pytest.raises(ValueError):
        plan([row, second], [record], {})


def test_capture_hash_url_status_and_path_validation(tmp_path):
    body = b'{"results":[{}]}'
    (tmp_path / "body").write_bytes(body)
    meta = dict(
        url="https://api.pbpstats.com/get-games/nba?Season=2025-26&SeasonType=Regular+Season",
        http_status=200,
        status="captured",
        sha256=sha(body),
        started_at="2025-10-23T00:00:00+00:00",
    )
    (tmp_path / "meta").write_text(json.dumps(meta))
    assert pbp_records(tmp_path, {"body": "body", "capture": "meta"}, "2025-26")[0] == [
        {}
    ]
    meta["http_status"] = 403
    (tmp_path / "meta").write_text(json.dumps(meta))
    with pytest.raises(ValueError):
        pbp_records(tmp_path, {"body": "body", "capture": "meta"}, "2025-26")
    with pytest.raises(ValueError):
        pbp_records(tmp_path, {"body": "../outside", "capture": "meta"}, "2025-26")


def test_production_destination_rejected():
    with pytest.raises(ValueError):
        publication_sql("test-project.nba_gold")
    sql = publication_sql("test-project." + DATASET)
    assert "BEGIN TRANSACTION" in sql and "stale_mapping_review" in sql
    assert "COMMIT TRANSACTION" in sql and "mapping_conflict" in sql
