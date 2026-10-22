from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest
from scripts.gamebook_store import GamebookStore, canonical, clock_seconds, sha

from gamebook_storage_scenarios import GAME, candidate, counts, ingest, run_scenarios


@pytest.fixture
def store(tmp_path):
    value = GamebookStore(tmp_path / "store")
    value.register_games([GAME])
    yield value
    value.close()


def test_replay_correction_failure_and_pinned_report(tmp_path):
    assert run_scenarios(tmp_path / "scenarios")["passed"]


def test_identical_replay_leaves_facts_and_publication_unchanged(store, tmp_path):
    data = candidate(tmp_path / "input")
    assert ingest(store, data)["status"] == "published"
    before = counts(store)
    publication = store.rows("SELECT * FROM gamebook_publication_history")
    for _ in range(3):
        assert ingest(store, data)["status"] == "noop"
    after = counts(store)
    assert after.pop("gamebook_extraction_runs") == 4
    before.pop("gamebook_extraction_runs")
    assert before == after
    assert store.rows("SELECT * FROM gamebook_publication_history") == publication


def test_failure_during_publication_rolls_back_candidate_history_and_pointer(
    store, tmp_path, monkeypatch
):
    original = candidate(tmp_path / "original")
    correction = candidate(tmp_path / "corrected", assisted=False)
    ingest(store, original)
    before = counts(store)
    real_publish = store._publish

    def crash_after_writes(attempt, revision):
        real_publish(attempt, revision)
        raise sqlite3.OperationalError("Injected failure after pointer/history writes")

    monkeypatch.setattr(store, "_publish", crash_after_writes)
    result = ingest(store, correction)
    assert result["status"] == "failed"
    after = counts(store)
    after.pop("gamebook_extraction_runs")
    before.pop("gamebook_extraction_runs")
    assert before == after
    assert (
        store.coverage(GAME["game_id"])["current_revision"]
        == original[0]["extraction"]["revision"]
    )
    assert store.coverage(GAME["game_id"])["health"] == "failed"
    monkeypatch.setattr(store, "_publish", real_publish)
    assert ingest(store, correction)["status"] == "published"
    assert store.coverage(GAME["game_id"])["health"] == "healthy"


def test_stale_publisher_cannot_overwrite_current_revision(store, tmp_path):
    first = candidate(tmp_path / "first")
    correction = candidate(tmp_path / "corrected", assisted=False)
    ingest(store, first, expected_previous=None)
    old = first[0]["extraction"]["revision"]
    ingest(store, correction, expected_previous=old)
    result = ingest(store, first, expected_previous=old)
    assert result["status"] == "conflict"
    assert (
        store.coverage(GAME["game_id"])["current_revision"]
        == correction[0]["extraction"]["revision"]
    )
    assert counts(store)["gamebook_publication_history"] == 2


def test_old_revision_cannot_be_republished_as_new_correction(store, tmp_path):
    first = candidate(tmp_path / "first")
    ingest(store, first)
    ingest(store, candidate(tmp_path / "corrected", assisted=False))
    assert ingest(store, first)["status"] == "rejected"
    assert counts(store)["current_event_relationships"] == 0


def test_new_parser_revision_can_rebuild_same_pdf(store, tmp_path):
    data = candidate(tmp_path / "input")
    ingest(store, data)
    revised = copy.deepcopy(data[0])
    extraction = revised["extraction"]
    extraction["parser_version"] = "synthetic_parser/2"
    extraction["parser_sha256"] = "e" * 64
    extraction["revision"] = sha(
        canonical({k: v for k, v in extraction.items() if k != "revision"}).encode()
    )
    for event in revised["scoring_events"]:
        event["revision"] = extraction["revision"]
        event["event_id"] = sha(
            f"{event['revision']}:{event['page']}:{event['row']}:{event['team']}".encode()
        )
    assert ingest(store, (revised, data[1], data[2]))["status"] == "published"
    total = counts(store)
    assert total["gamebook_documents"] == 1
    assert total["gamebook_revisions"] == 2
    assert total["event_relationships"] == 2
    assert total["current_event_relationships"] == 1


@pytest.mark.parametrize(
    "change",
    [
        "drop_event",
        "wrong_scope",
        "box_mismatch",
        "assist_subset",
        "bbox",
        "outside_pdf",
        "same_revision_changed_output",
        "missing_period",
    ],
)
def test_corrupted_candidates_never_replace_valid_data(store, tmp_path, change):
    data = candidate(tmp_path / "input")
    ingest(store, data)
    bad = copy.deepcopy(data[0])
    if change == "drop_event":
        bad["scoring_events"] = []
    elif change == "wrong_scope":
        bad["scoring_events"][0]["season"] = "2024-25"
    elif change == "box_mismatch":
        bad["player_validation"][0]["stats"]["A"] += 1
    elif change == "assist_subset":
        bad["assists"] = []
    elif change == "bbox":
        bad["scoring_events"][0]["bbox"] = [0, 0, -1, 5]
    elif change == "outside_pdf":
        # Modify the shared coordinate list so retained rows still agree with the event.
        bad["scoring_events"][0]["bbox"][2] = 99999
    elif change == "same_revision_changed_output":
        bad["player_validation"][0]["box_y"] += 1
    else:
        bad["rows"] = [r for r in bad["rows"] if r["period"] != 4]
    result = ingest(store, (bad, data[1], data[2]))
    assert result["status"] == "rejected", result
    assert counts(store)["current_event_relationships"] == 1
    assert counts(store)["gamebook_publication_history"] == 1


@pytest.mark.parametrize("part", ["pdf", "identity"])
def test_source_hash_mismatch_is_visible_and_retains_previous(store, tmp_path, part):
    data = candidate(tmp_path / "input")
    ingest(store, data)
    altered = list(data)
    altered[1 if part == "pdf" else 2] += b"changed"
    assert ingest(store, altered)["status"] == "rejected"
    assert counts(store)["current_event_relationships"] == 1
    assert store.coverage(GAME["game_id"])["last_failure_attempt_id"] is not None


def test_unreadable_pdf_with_matching_hash_records_rejection(store, tmp_path):
    data = candidate(tmp_path / "input")
    ingest(store, data)
    bad = copy.deepcopy(data[0])
    body = b"not a PDF"
    extraction = bad["extraction"]
    extraction["document_sha256"] = sha(body)
    extraction["revision"] = sha(
        canonical({k: v for k, v in extraction.items() if k != "revision"}).encode()
    )
    assert ingest(store, (bad, body, data[2]))["status"] == "rejected"
    assert counts(store)["current_event_relationships"] == 1
    assert store.coverage(GAME["game_id"])["health"] == "failed"


def test_missing_game_is_pending_then_recovers(store, tmp_path):
    assert store.coverage(GAME["game_id"])["health"] == "pending"
    assert store.coverage(GAME["game_id"])["current_revision"] is None
    rejected = {
        "game": GAME,
        "status": "rejected",
        "errors": [{"error": "not retrieved"}],
    }
    assert store.ingest(rejected)["status"] == "rejected"
    assert store.coverage(GAME["game_id"])["current_revision"] is None
    assert ingest(store, candidate(tmp_path / "input"))["status"] == "published"
    assert store.coverage(GAME["game_id"])["health"] == "healthy"


def test_correction_changes_only_its_game(store, tmp_path):
    first = candidate(tmp_path / "first")
    second = copy.deepcopy(first)
    second[0]["game"]["game_id"] = "0000000002"
    for event in second[0]["scoring_events"]:
        event["game_id"] = "0000000002"
    store.register_games([second[0]["game"]])
    ingest(store, first)
    ingest(store, second)
    other_before = store.coverage("0000000002")
    assert counts(store)["current_event_relationships"] == 2
    assert (
        ingest(store, candidate(tmp_path / "corrected", assisted=False))["status"]
        == "published"
    )
    assert counts(store)["current_event_relationships"] == 1
    assert store.coverage("0000000002") == other_before
    assert store.rows("SELECT game_id FROM current_event_relationships") == [
        {"game_id": "0000000002"}
    ]


def test_content_addressed_artifact_is_never_overwritten(store):
    content = b"synthetic immutable bytes"
    location = store.artifact("test", content, "txt")
    path = store.root / location
    assert store.artifact("test", content, "txt") == location
    path.write_bytes(b"simulated disk corruption")
    with pytest.raises(ValueError, match="Immutable artifact differs"):
        store.artifact("test", content, "txt")
    assert path.read_bytes() == b"simulated disk corruption"


def test_export_hashes_links_and_membership_are_consistent(store, tmp_path):
    ingest(store, candidate(tmp_path / "input"))
    destination = tmp_path / "export"
    manifest = store.export(destination)
    for name, info in manifest["tables"].items():
        content = (destination / name).read_bytes()
        assert sha(content) == info["sha256"]
        assert len(content.splitlines()) == info["rows"]
    report = json.loads((destination / "pair_report.json").read_text())
    assert report["pairs"][0]["revision"] == report["coverage"][0]["current_revision"]
    assert report["snapshot_id"] == manifest["report_snapshot_id"]
    doc = store.rows("SELECT * FROM gamebook_documents")[0]
    assert (
        sha((store.root / doc["artifact_path"]).read_bytes()) == doc["document_sha256"]
    )
    revision = store.rows("SELECT * FROM gamebook_revisions")[0]
    assert Path(store.root / revision["payload_path"]).exists()
    assert store.rows("PRAGMA foreign_key_check") == []
    with pytest.raises(FileExistsError):
        store.export(destination)


@pytest.mark.parametrize(
    "clock,expected", [(":38.4", 38.4), ("12:00", 720), ("0:00", 0)]
)
def test_fractional_clock_units(clock, expected):
    assert clock_seconds(clock) == expected


@pytest.mark.parametrize("clock", ["12:01", "11:60", "banana"])
def test_invalid_clock_rejected(clock):
    with pytest.raises(ValueError):
        clock_seconds(clock)
