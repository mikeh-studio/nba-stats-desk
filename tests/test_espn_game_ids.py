import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import verify_espn_game_ids as verify
from scripts.fetch_gamebooks import source_url
from tests.support.espn import fixtures

from test_gamebook_assists import box_page, fragment, pdf_from_pages


def bundle(tmp_path):
    _, summary = fixtures()
    c = {
        "source_game_id": "1234567",
        "nba_game_id_candidate": "0022500001",
        "season": "2025-26",
        "date": "2025-10-22",
        "away": "BOS",
        "home": "NYK",
        "summary_attempt": "summary/attempt.json",
        "pdf_attempt": "pdf/attempt.json",
    }
    page = {
        "method": "operator_reviewed_web_tool",
        "observed_at": "2025-10-23T00:00:00+00:00",
        "title": "Boston Celtics vs New York Knicks Oct 22, 2025 Box Scores | NBA.com",
        "url": "https://www.nba.com/game/bos-vs-nyk-0022500001/box-score",
        "capture": "page.txt",
    }
    body = (page["title"] + " (" + page["url"] + ")").encode()
    page["sha256"] = verify.sha(body)
    (tmp_path / "page.txt").write_bytes(body)
    c["page"] = page
    (tmp_path / "summary").mkdir()
    body = json.dumps(summary).encode()
    (tmp_path / "summary/response.body").write_bytes(body)
    (tmp_path / "summary/attempt.json").write_text(
        json.dumps(
            {
                "status": "captured",
                "http_status": 200,
                "sha256": verify.sha(body),
                "url": verify.source.BASE + "/summary?event=1234567",
            }
        )
    )
    fragments = box_page() + [
        fragment(10, 35, "Wednesday, October 22, 2025"),
        fragment(75, 50, "Boston Celtics (0-1)"),
        fragment(75, 200, "NEW YORK KNICKS (1-0)"),
    ]
    body = pdf_from_pages([fragments])
    (tmp_path / "pdf").mkdir()
    (tmp_path / "pdf/book.pdf").write_bytes(body)
    game = {k: c[k] for k in ("date", "season", "home", "away")}
    game.update(
        game_id=c["nba_game_id_candidate"],
        home_name="New York Knicks",
        away_name="Boston Celtics",
    )
    (tmp_path / "pdf/attempt.json").write_text(
        json.dumps(
            {
                "status": "available",
                "http_status": 200,
                "body_complete": True,
                "game": game,
                "sha256": verify.sha(body),
                "url": source_url(game),
                "artifact": "book.pdf",
            }
        )
    )
    return c


def test_identity_verification_and_identical_replay(tmp_path):
    c = bundle(tmp_path)
    first = verify.verify_bundle(c, tmp_path)
    assert first == verify.verify_bundle(c, tmp_path)
    assert first["record"]["GameId"] == "0022500001"
    assert first["record"]["HomePoints"] == 3
    assert first["record"]["method"] == verify.METHOD


@pytest.mark.parametrize(
    "field,value",
    [
        ("home", "DET"),
        ("date", "2025-10-23"),
        ("season", "2024-25"),
        ("source_game_id", "7654321"),
        ("nba_game_id_candidate", "0022500002"),
    ],
)
def test_rejects_candidate_scope_changes(tmp_path, field, value):
    c = bundle(tmp_path)
    c[field] = value
    assert verify.verify_all([c], tmp_path)[0]["status"] == "rejected"


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "network_error"),
        ("body_complete", False),
        ("sha256", "bad"),
        ("url", "https://example.test/book.pdf"),
    ],
)
def test_rejects_unavailable_or_corrupt_pdf_capture(tmp_path, field, value):
    c = bundle(tmp_path)
    p = tmp_path / "pdf/attempt.json"
    meta = json.loads(p.read_text())
    meta[field] = value
    p.write_text(json.dumps(meta))
    assert verify.verify_all([c], tmp_path)[0]["status"] == "rejected"


def test_rejects_pdf_score_disagreement(tmp_path):
    c = bundle(tmp_path)
    p = tmp_path / "summary/response.body"
    payload = json.loads(p.read_text())
    payload["header"]["competitions"][0]["competitors"][0]["score"] = "4"
    p.write_text(json.dumps(payload))
    meta = json.loads(p.with_name("attempt.json").read_text())
    meta["sha256"] = verify.sha(p.read_bytes())
    p.with_name("attempt.json").write_text(json.dumps(meta))
    result = verify.verify_all([c], tmp_path)[0]
    assert result["status"] == "rejected" and "score conflict" in result["error"]


def test_pdf_must_bind_home_away_roles_not_just_contain_both_names(tmp_path):
    c = bundle(tmp_path)
    meta = json.loads((tmp_path / c["pdf_attempt"]).read_text())
    game = meta["game"]
    game["home_name"], game["away_name"] = game["away_name"], game["home_name"]
    with pytest.raises(ValueError, match="home/away role"):
        verify.pdf_box_identity((tmp_path / "pdf/book.pdf").read_bytes(), game)


def test_rejects_unbound_page_title_and_changed_capture(tmp_path):
    c = bundle(tmp_path)
    c["page"]["title"] = (
        "Boston Celtics vs Detroit Pistons Oct 22, 2025 Box Scores | NBA.com"
    )
    assert verify.verify_all([c], tmp_path)[0]["status"] == "rejected"
    c = copy.deepcopy(c)
    c["page"]["sha256"] = "bad"
    assert "hash mismatch" in verify.verify_all([c], tmp_path)[0]["error"]


def test_duplicate_and_outside_evidence_paths_rejected(tmp_path):
    c = bundle(tmp_path)
    with pytest.raises(ValueError, match="Duplicate"):
        verify.verify_all([c, c], tmp_path)
    with pytest.raises(ValueError, match="evidence path"):
        verify.local_file(tmp_path, "../secret")


def test_identity_does_not_claim_to_validate_pbp(tmp_path):
    c = bundle(tmp_path)
    p = tmp_path / "summary/response.body"
    payload = json.loads(p.read_text())
    payload["plays"] = []
    p.write_text(json.dumps(payload))
    meta = json.loads(p.with_name("attempt.json").read_text())
    meta["sha256"] = verify.sha(p.read_bytes())
    p.with_name("attempt.json").write_text(json.dumps(meta))
    result = verify.verify_bundle(c, tmp_path)
    assert result["status"] == "verified"
    assert "identity and final score only" in result["record"]["scope"]


@pytest.mark.parametrize("available", [True, False])
def test_cli_archives_before_publishing_and_never_publishes_rejections(
    tmp_path, monkeypatch, available
):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    c = bundle(inputs)
    if not available:
        path = inputs / c["pdf_attempt"]
        meta = json.loads(path.read_text())
        meta["status"] = "network_error"
        path.write_text(json.dumps(meta))
    (inputs / "candidates.json").write_text(json.dumps([c]))
    calls = []

    def archive(cloud, directory):
        assert (directory / "verification.json").exists()
        calls.append("archive")
        return "gs://synthetic/dev/evidence.json"

    store = SimpleNamespace(
        cloud=None, remember_mapping=lambda *args: calls.append("mapping")
    )
    monkeypatch.setitem(
        sys.modules,
        "scripts.espn_ingestion",
        SimpleNamespace(make_store=lambda *args: store),
    )
    monkeypatch.setitem(
        sys.modules,
        "scripts.gamebook_ingestion",
        SimpleNamespace(archive_sources=archive),
    )
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    out = tmp_path / "reports/output"
    monkeypatch.setattr(
        sys,
        "argv",
        ["verify", "--bundle", str(inputs), "--output", str(out), "--publish-dev"],
    )
    assert verify.main() == (0 if available else 2)
    assert calls == (["archive", "mapping"] if available else ["archive"])
    result = json.loads((out / "result.json").read_text())
    assert result[0]["status"] == ("verified" if available else "rejected")
