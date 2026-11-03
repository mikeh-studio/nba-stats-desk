import json
from types import SimpleNamespace

import pytest
from scripts import espn_game_mapping_weekly as worker
from scripts.gamebook_sources import SourceUnavailable

from test_espn_game_mapping_recovery import inputs


def runtime(monkeypatch, tmp_path, mode="matched"):
    row, record, _, _ = inputs()
    calls = []
    observed = dict(
        row,
        nba_game_id=record["GameId"],
        nba_game_mapping_status="resolved",
        review_status=None,
        review_detail=None,
    )

    def query(sql, params=()):
        calls.append(sql)
        if sql.startswith("SELECT g."):
            return [observed]
        return [{"inserted": 1}] if sql.startswith("BEGIN") else []

    cloud = SimpleNamespace(
        query=query,
        parameter=lambda k, v: (k, v),
        upload=lambda *a: "gs://synthetic/review",
    )
    store = SimpleNamespace(
        prefix="test-project." + worker.DATASET, cloud=cloud, coverage=lambda s: [row]
    )

    def capture(url, directory, session):
        directory.mkdir()
        meta = {
            "http_status": 403
            if mode == "denied"
            else None
            if mode == "timeout"
            else 200,
            "error_type": "ReadTimeout" if mode == "timeout" else None,
            "finished_at": "2025-10-23T12:00:00+00:00",
        }
        (directory / "attempt.json").write_text(json.dumps(meta))
        if mode in ("denied", "timeout"):
            raise SourceUnavailable("denied")
        return {"results": [record]} if mode != "invalid" else {"results": {}}

    monkeypatch.setattr(worker, "capture_json", capture)
    monkeypatch.setattr(
        "scripts.gamebook_ingestion.archive_sources", lambda *a: "gs://synthetic/source"
    )
    return store, calls


def test_matched_feed_publishes_only_alias_transaction(tmp_path, monkeypatch):
    store, calls = runtime(monkeypatch, tmp_path)
    result = worker.discover(store, object(), tmp_path, "2025-26")
    assert result["inserted"] == 1 and result["source_status"] == "captured"
    assert result["snapshot"]["resolved"] == 1
    sql = next(s for s in calls if s.startswith("BEGIN"))
    assert "INSERT INTO" in sql and ".espn_mappings`" in sql
    assert "INSERT INTO `test-project.nba_gold" not in sql


@pytest.mark.parametrize(
    "mode,status",
    [
        ("denied", "access_denied"),
        ("invalid", "invalid_payload"),
        ("timeout", "timeout"),
    ],
)
def test_failed_acquisition_retains_aliases_and_does_not_publish(
    tmp_path, monkeypatch, mode, status
):
    store, calls = runtime(monkeypatch, tmp_path, mode)
    result = worker.discover(store, object(), tmp_path, "2025-26")
    assert result["status"] == "degraded" and result["source_status"] == status
    assert result["snapshot"]["resolved"] == 1
    assert not any(s.startswith("BEGIN") for s in calls)


def test_snapshot_keeps_separately_verified_identity_and_scoring_conflict():
    row, record, _, _ = inputs()
    row.update(
        nba_game_id=record["GameId"],
        nba_game_mapping_status="resolved",
        review_status="mismatch",
        identity_method="reviewed_nba_visible_final_v1",
        review_detail=json.dumps(
            {
                "error": "Mapping score conflict",
                "provider_records": [dict(record, AwayPoints=97)],
            }
        ),
    )
    result = worker.summarize([row])
    assert result["resolved"] == 1 and result["discrepancy_count"] == 1
    assert result["discrepancies"][0]["identity_verified_separately"]
    assert result["discrepancies"][0]["provider_away_score"] == 97


def test_duplicate_observation_cannot_inflate_mapping_count():
    row, _, _, _ = inputs()
    row["nba_game_mapping_status"] = "resolved"
    with pytest.raises(ValueError):
        worker.summarize([row, row])
