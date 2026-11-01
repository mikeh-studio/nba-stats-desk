import copy
import json
from types import SimpleNamespace

import pytest
from scripts import espn_only_store as only
from scripts.espn_only_ingestion import read_capture
from scripts.espn_sources import scoreboard
from scripts.espn_store import validate as legacy_validate
from scripts.fetch_gamebooks import digest
from tests.support.espn import DAY, SEASON, fixtures


def candidate():
    schedule, summary = fixtures()
    return only.normalize(summary, scoreboard(schedule, SEASON, DAY)[0])


def test_source_only_is_strict_and_cannot_enter_legacy_store():
    data = candidate()
    assert only.validate(data)
    assert data["reconciliation_status"] == "reconciled"
    assert data["nba_game_id"] is None
    assert data["pdf_validation"]["status"] == "not_attempted"
    with pytest.raises(ValueError):
        legacy_validate(data)


@pytest.mark.parametrize(
    "field",
    ["assists", "play_count", "pdf_validation", "nba_game_id", "reconciliation_status"],
)
def test_cannot_forge_candidate_fields(field):
    data = candidate()
    data[field] = [] if field == "assists" else "forged"
    with pytest.raises((ValueError, TypeError)):
        only.validate(data)


def test_corrected_event_changes_revision_but_media_and_participant_order_do_not():
    schedule, payload = fixtures()
    game = scoreboard(schedule, SEASON, DAY)[0]
    original = only.normalize(payload, game)
    payload["news"] = "irrelevant"
    payload["plays"][0]["participants"].reverse()
    assert only.validate(only.normalize(payload, game)) == only.validate(original)
    payload["plays"][0]["clock"]["displayValue"] = "10:59"
    assert only.validate(only.normalize(payload, game)) != only.validate(original)


def test_internal_source_corruption_rejected_even_if_status_claims_reconciled():
    data = candidate()
    data["reconciliation_input"]["plays"][-1]["homeScore"] = 99
    with pytest.raises(ValueError, match="Final PBP"):
        only.validate(data)


@pytest.mark.parametrize(
    "dataset", ["nba_gold", "nba_gamebook_dev_espn", "nba_gamebook_dev"]
)
def test_only_store_rejects_other_destinations(dataset):
    with pytest.raises(ValueError, match="isolated"):
        only.ESPNOnlyStore(SimpleNamespace(dataset=dataset))


def test_capture_hash_url_and_success_are_required(tmp_path):
    body = b'{"plays":[]}'
    meta = {
        "sha256": digest(body),
        "url": "https://example.test/capture",
        "status": "captured",
        "http_status": 200,
    }
    (tmp_path / "response.body").write_bytes(body)
    (tmp_path / "attempt.json").write_text(json.dumps(meta))
    assert read_capture(tmp_path, meta["url"]) == {"plays": []}
    with pytest.raises(ValueError, match="URL"):
        read_capture(tmp_path, "https://different.test")
    (tmp_path / "response.body").write_bytes(body + b" ")
    with pytest.raises(ValueError, match="hash"):
        read_capture(tmp_path, meta["url"])


def test_failed_validation_preserves_source_input():
    data = candidate()
    before = copy.deepcopy(data)
    only.validate(data)
    assert data == before


def test_pdf_cannot_verify_using_conflicting_player_mapping(tmp_path):
    from scripts.espn_only_ingestion import verify_pdf
    from tests.support.espn import REF

    cloud = SimpleNamespace(
        query=lambda *args: [{"source_player_id": "100", "nba_player_id": "99"}],
        parameter=lambda *args: args,
    )
    store = SimpleNamespace(
        cloud=cloud, prefix="test-project.nba_gamebook_dev_espn_only"
    )
    with pytest.raises(ValueError, match="accepted player crosswalk"):
        verify_pdf(store, candidate(), tmp_path, tmp_path, REF)


@pytest.mark.parametrize("defect", ["catalog", "pdf", "source"])
def test_enrichment_failure_does_not_gate_valid_facts(tmp_path, monkeypatch, defect):
    from scripts import espn_only_ingestion as worker
    from scripts.gamebook_sources import SourceUnavailable

    schedule, payload = fixtures()
    if defect == "source":
        payload["plays"][-1]["homeScore"] = 99
    capture = tmp_path / "capture"

    def save(directory, data, url):
        directory.mkdir(parents=True)
        body = json.dumps(data).encode()
        (directory / "response.body").write_bytes(body)
        (directory / "attempt.json").write_text(
            json.dumps(
                {
                    "url": url,
                    "sha256": digest(body),
                    "status": "captured",
                    "http_status": 200,
                }
            )
        )

    save(
        capture / "sources" / str(DAY),
        schedule,
        worker.source.BASE + "/scoreboard?dates=" + DAY.strftime("%Y%m%d"),
    )
    save(
        capture / "games" / "1234567" / "source",
        payload,
        worker.source.BASE + "/summary?event=1234567",
    )
    calls = []
    cloud = SimpleNamespace(
        project="test-project",
        parameter=lambda *args: args,
        query=lambda *args: [],
        upload=lambda *args: "gs://synthetic/evidence",
    )

    def publish(data, evidence):
        only.validate(data)
        calls.append("published")
        return {"status": "published"}

    store = SimpleNamespace(
        cloud=cloud,
        prefix="test-project.nba_gamebook_dev_espn_only",
        register_games=lambda games: None,
        publish=publish,
        failure=lambda *args: {"status": "rejected"},
        record_check=lambda data, kind, status, *args: calls.append((kind, status)),
        remember_players=lambda *args: {"status": "published"},
    )

    def catalog(*args):
        if defect == "catalog":
            raise SourceUnavailable("synthetic catalog outage")
        from tests.support.espn import REF

        return REF, {"method": "synthetic", "sha256": "synthetic"}

    monkeypatch.setattr(worker.identity, "fetch_catalog", catalog)
    monkeypatch.setattr(
        worker, "archive_sources", lambda *args: "gs://synthetic/evidence"
    )

    def unavailable(*args):
        raise FileNotFoundError("synthetic missing PDF")

    monkeypatch.setattr(worker, "verify_pdf", unavailable)
    output = tmp_path / "output"
    output.mkdir()
    report = worker.ingest(
        store,
        capture,
        output,
        SEASON,
        DAY,
        DAY,
        None,
        {"1234567": "missing"} if defect == "pdf" else None,
    )
    if defect == "source":
        assert report["games"][0]["status"] == "rejected"
        assert "published" not in calls
    else:
        assert report["games"][0]["status"] == "published"
        assert "published" in calls
        assert (
            ("player_mapping", "failed") in calls
            if defect == "catalog"
            else ("pdf", "unavailable") in calls
        )
