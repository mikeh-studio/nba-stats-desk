"""Played-date reviews never silently rewrite archive inputs or confer coverage."""

import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from app.data.unified.identity import ContractError, canonical
from app.data.unified.store import ObservationStore
from jsonschema import Draft202012Validator, FormatChecker
from tests.support.historical import ingest, payload, receipt


def reviewed():
    original = payload()
    original["game"]["game_date"] = "2025-10-21"
    body = canonical(original).encode()
    url = "https://www.nba.com/game/bos-vs-nyk-0022500001"
    page = f"Boston Celtics vs New York Knicks Oct 22, 2025 Game Summary | NBA.com ({url})\nL132: Wednesday, October 22nd, 2025\n"
    return {
        "schema_version": "nba_archive_date_review/1",
        "provider": "nba_api",
        "season": "2025-26",
        "phase": "Regular Season",
        "game": copy.deepcopy(original["game"]),
        "original_body": body.decode(),
        "original_receipt": receipt(body),
        "review": {
            "schema_version": "reviewed_nba_played_date/1",
            "game_id": original["game"]["game_id"],
            "original_input_sha256": hashlib.sha256(body).hexdigest(),
            "old_date": "2025-10-21",
            "played_date": "2025-10-22",
            "scope": {
                "league": "nba",
                "season": "2025-26",
                "phase": "Regular Season",
                "date": "2025-10-22",
                "home": "NYK",
                "away": "BOS",
            },
            "authority": {
                "method": "operator_reviewed_nba_game_page_web_tool_v1",
                "url": url,
                "body": page,
                "receipt": receipt(page.encode(), "2026-10-30T00:00:00+00:00"),
            },
        },
    }


def test_review_projects_only_game_date_and_preserves_original_and_eligibility():
    store = ObservationStore()
    value = reviewed()
    before = copy.deepcopy(value)
    result = ingest(store, value)
    obs = store.read_observation(result["observation_id"])
    assert value == before
    assert obs["semantic"]["scope"]["date"] == "2025-10-22"
    assert obs["semantic"]["date_basis"] == "reviewed_nba_played_date/1"
    assert obs["date_review"]["review"]["old_date"] == "2025-10-21"
    assert obs["date_review"]["original_receipt"]["captured_at"] is None
    assert obs["semantic"]["membership_level"] == "observed_sample"
    assert not any(obs["semantic"]["capabilities"].values())
    assert ingest(store, value)["status"] == "noop"
    with pytest.raises(ContractError, match="Game scope changed"):
        ingest(store, json.loads(value["original_body"]))


@pytest.mark.parametrize(
    "mode",
    [
        "hash",
        "wrong_game",
        "date",
        "title",
        "clock",
        "method",
        "scope",
        "row_date",
        "scores",
        "membership",
    ],
)
def test_invalid_review_fails_closed_without_partial_identity(mode):
    store = ObservationStore()
    value = reviewed()
    authority = value["review"]["authority"]
    if mode == "hash":
        value["original_receipt"]["sha256"] = "0" * 64
    elif mode == "wrong_game":
        authority["url"] += "0"
    elif mode in ("date", "title"):
        authority["body"] = (
            authority["body"].replace("22nd", "23rd")
            if mode == "date"
            else authority["body"].replace("Boston", "Chicago")
        )
        authority["receipt"] = receipt(
            authority["body"].encode(), "2026-10-30T00:00:00+00:00"
        )
    elif mode == "clock":
        authority["receipt"]["captured_at"] = None
    elif mode == "method":
        authority["method"] = "http_200"
    elif mode == "scope":
        value["review"]["scope"]["home"] = "MIL"
    else:
        original = json.loads(value["original_body"])
        if mode == "row_date":
            original["appearances"][0]["game_date"] = "2025-10-23"
        elif mode == "scores":
            original["game"]["home_team_pts"] += 2
        elif mode == "membership":
            original["membership"] = {}
        body = canonical(original).encode()
        value["original_body"] = body.decode()
        value["game"] = copy.deepcopy(original["game"])
        value["original_receipt"] = receipt(body)
        value["review"]["original_input_sha256"] = hashlib.sha256(body).hexdigest()
    with pytest.raises(ValueError):
        ingest(store, value)
    assert store.current_semantics() == []
    assert store.connection.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0


def test_resolution_is_append_only_bound_to_exact_quarantine_and_idempotent():
    store = ObservationStore()
    value = reviewed()
    body = value["original_body"].encode()
    qid = store.quarantine_bytes(
        body, value["original_receipt"], "Appearance scope differs"
    )
    first = ingest(store, value)
    store.resolve_date_quarantine(qid, first["observation_id"])
    store.resolve_date_quarantine(qid, first["observation_id"])
    assert store.unresolved_quarantines() == []
    assert (
        store.connection.execute(
            "SELECT COUNT(*) FROM quarantine_resolutions"
        ).fetchone()[0]
        == 1
    )
    assert (
        store.connection.execute("SELECT payload FROM quarantined_inputs").fetchone()[0]
        == value["original_body"]
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store.connection.execute("DELETE FROM quarantine_resolutions")
    other = copy.deepcopy(value["original_receipt"])
    other["reference"] = "another-receipt"
    qid2 = store.quarantine_bytes(body, other, "Appearance scope differs")
    with pytest.raises(ContractError, match="binding differs"):
        store.resolve_date_quarantine(qid2, first["observation_id"])


def test_review_does_not_bypass_expected_revision_or_participant_guards():
    store = ObservationStore()
    value = reviewed()
    baseline = ingest(store, payload())
    with pytest.raises(ContractError, match="expected prior"):
        ingest(store, value)
    with pytest.raises(ContractError, match="participant evidence"):
        ingest(store, value, expected=baseline["revision"])
    assert store.current_revision(baseline["game_uid"]) == baseline["revision"]


def test_review_and_output_schema():
    store = ObservationStore()
    value = reviewed()
    result = ingest(store, value)
    folder = Path(__file__).resolve().parents[1] / "contracts/unified_data"
    for filename, document in [
        ("nba-archive-date-review.schema.json", value),
        ("observation.schema.json", store.read_observation(result["observation_id"])),
    ]:
        schema = json.loads((folder / filename).read_text())
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
