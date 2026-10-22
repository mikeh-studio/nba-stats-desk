from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import requests
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import fetch_gamebooks as fetch

GAME = {
    "game_id": "0000000001",
    "date": "2025-10-22",
    "season": "2025-26",
    "away": "AAA",
    "home": "BBB",
    "away_name": "Example Visitors",
    "home_name": "Example Hosts",
}


def pdf(text="Example Visitors Example Hosts October 22, 2025 PLAY-BY-PLAY"):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 10 Tf 10 700 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = stream
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


class Response:
    def __init__(self, status=200, body=b"", failure=None):
        self.status_code = status
        self.headers = {"Content-Type": "application/pdf"}
        self.body = body
        self.failure = failure

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, chunk_size):
        yield self.body
        if self.failure:
            raise self.failure


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_available_requires_pdf_identity_and_pbp(tmp_path):
    body = pdf()
    session = Session(Response(body=body))
    record = fetch.fetch_one(GAME, tmp_path, session, 1)
    assert record["status"] == "available"
    assert record["pbp_marker_pages"] == [1]
    assert record["identity_verified"]
    assert record["sha256"] == fetch.digest(body)
    assert (tmp_path / record["artifact"]).read_bytes() == body
    assert json.loads((tmp_path / "attempt.json").read_text()) == record
    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (403, "http_blocked"),
        (429, "http_blocked"),
        (404, "not_found"),
        (500, "http_error"),
        (302, "http_error"),
        (200, "invalid_pdf"),
    ],
)
def test_html_never_counts_as_pdf_and_http_errors_are_preserved(tmp_path, code, status):
    body = b"<html>Unavailable</html>"
    session = Session(Response(code, body))
    record = fetch.fetch_one(GAME, tmp_path, session, 1)
    assert record["status"] == status
    assert record["http_status"] == code
    assert (tmp_path / record["artifact"]).read_bytes() == body
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "text",
    [
        "Example Visitors Different Hosts October 22, 2025 PLAY-BY-PLAY",
        "Example Visitors Example Hosts October 23, 2025 PLAY-BY-PLAY",
    ],
)
def test_wrong_identity_keeps_document_but_not_available(tmp_path, text):
    record = fetch.fetch_one(GAME, tmp_path, Session(Response(body=pdf(text))), 1)
    assert record["status"] == "identity_unverified"
    assert record["pdf_valid"] is True
    assert record["identity_verified"] is False


def test_boxscore_without_pbp_is_not_ready(tmp_path):
    record = fetch.fetch_one(
        GAME,
        tmp_path,
        Session(Response(body=pdf("Example Visitors Example Hosts October 22, 2025"))),
        1,
    )
    assert record["status"] == "pbp_unverified"


def test_transport_failure_has_no_fake_status_or_hash(tmp_path):
    record = fetch.fetch_one(GAME, tmp_path, Session(requests.ConnectTimeout()), 1)
    assert record["http_status"] is None
    assert record["sha256"] is None
    assert record["status"] == "network_error"
    assert record["body_complete"] is False


def test_interrupted_body_retains_partial_evidence(tmp_path):
    record = fetch.fetch_one(
        GAME,
        tmp_path,
        Session(Response(body=b"partial", failure=requests.ReadTimeout())),
        1,
    )
    assert record["http_status"] == 200
    assert record["status"] == "network_error"
    assert record["body_complete"] is False
    assert (tmp_path / record["artifact"]).read_bytes() == b"partial"


def test_response_size_bound_is_not_validated(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "MAX_BYTES", 4)
    record = fetch.fetch_one(GAME, tmp_path, Session(Response(body=b"12345")), 1)
    assert record["status"] == "size_limit"
    assert record["size_bytes"] == 4
    assert not record["body_complete"]


def test_bad_pdf_does_not_crash_inspection():
    assert fetch.inspect_pdf(b"%PDF-not-complete", GAME)["pdf_valid"] is False


def test_manifest_rejects_duplicates_and_out_of_season(tmp_path):
    path = tmp_path / "manifest.json"
    for games in [[GAME, GAME], [{**GAME, "season": "2024-25"}], []]:
        path.write_text(json.dumps(games))
        with pytest.raises(ValueError):
            fetch.load_manifest(path)
    path.write_text(json.dumps([GAME]))
    assert fetch.load_manifest(path) == [GAME]


def test_failed_games_remain_in_report_and_output_cannot_be_reused(tmp_path):
    output = tmp_path / "run"
    session = Session(Response(403, b"blocked"))
    games = [GAME, {**GAME, "game_id": "0000000002"}]
    report = fetch.run(games, output, "synthetic-test", 1, session)
    assert report["expected_games"] == 2
    assert report["available_games"] == 0
    assert len(report["attempts"]) == 2
    assert json.loads((output / "manifest.json").read_text()) == games
    with pytest.raises(FileExistsError):
        fetch.run(games, output, "synthetic-test", 1, session)
    assert len(session.calls) == 2
