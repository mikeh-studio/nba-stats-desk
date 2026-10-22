import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import espn_ingestion, gamebook_ingestion
from scripts.gamebook_publication import due_through, publication_report

from test_espn_pilot import DAY, SEASON, fixtures


@pytest.mark.parametrize(
    "clock,cutoff",
    [
        ("2026-10-22T03:59:59+00:00", "2026-10-20"),
        ("2026-10-22T04:00:00+00:00", "2026-10-21"),
        ("2026-11-02T04:59:59+00:00", "2026-10-31"),
        ("2026-11-02T05:00:00+00:00", "2026-11-01"),
        ("2027-01-01T05:00:00+00:00", "2026-12-31"),
    ],
)
def test_cutoff_is_previous_eastern_day_including_dst(clock, cutoff):
    assert str(due_through(datetime.fromisoformat(clock))) == cutoff


def test_clock_must_be_aware():
    with pytest.raises(ValueError, match="timezone aware"):
        due_through(datetime(2026, 10, 22))


def test_next_day_eligibility_preserves_old_failure_retry():
    games = [
        {"game_id": "old", "date": "2026-10-01"},
        {"game_id": "yesterday", "date": "2026-10-20"},
        {"game_id": "today", "date": "2026-10-21"},
    ]
    states = [
        {
            "game_id": "old",
            "last_status": "rejected",
            "last_checked": "2026-10-19T12:00:00+00:00",
        }
    ]
    clock = datetime(2026, 10, 22, 3, tzinfo=timezone.utc)
    selected, count = gamebook_ingestion.choose_games(games, states, clock)
    assert {g["game_id"] for g in selected} == {"old", "yesterday"}
    assert count == 2
    report = publication_report(games, clock, "game_id")
    assert report["publication_waits"] == [
        {
            "game_id": "today",
            "game_date": "2026-10-21",
            "expected_on": "2026-10-22",
            "status": "not_yet_due",
        }
    ]
    selected, count = gamebook_ingestion.choose_games(
        games, states, clock.replace(hour=4)
    )
    assert count == 3 and games[2] in selected


@pytest.mark.parametrize("source", ["warehouse", "schedule"])
def test_pdf_discovery_keeps_same_day_coverage_but_does_not_queue(
    tmp_path, monkeypatch, source
):
    worker = gamebook_ingestion
    games = [{"game_id": "0022600001", "date": "2026-10-21"}]
    monkeypatch.setattr(worker.warehouse, "capture", lambda *a, **kw: games)
    monkeypatch.setattr(worker.sources, "discover", lambda *a: games)
    monkeypatch.setattr(worker, "archive_sources", lambda *a: "gs://synthetic/evidence")
    monkeypatch.setattr(worker, "coverage", lambda *a: [])
    registered = []
    monkeypatch.setattr(
        worker, "register_completed", lambda store, rows: registered.extend(rows)
    )
    store = SimpleNamespace(upload=lambda *a: None)
    clock = datetime(2026, 10, 22, 3, tzinfo=timezone.utc)
    assert (
        worker.run_discovery(
            tmp_path, "2026-27", clock, store, None, discovery_source=source
        )
        == []
    )
    assert registered == games
    report = json.loads((tmp_path / "discovery.json").read_text())
    assert (
        report["discovered"] == 1 and report["not_yet_due"] == 1 and report["due"] == 0
    )


@pytest.mark.parametrize(
    "clock,selected",
    [("2025-10-23T03:59:59+00:00", 0), ("2025-10-23T04:00:00+00:00", 1)],
)
def test_espn_waits_until_next_day_and_records_reason(
    tmp_path, monkeypatch, clock, selected
):
    schedule, _ = fixtures()
    worker = espn_ingestion
    registered = []
    store = SimpleNamespace(
        cloud=SimpleNamespace(
            parameter=lambda *a: a,
            query=lambda *a: [],
            upload=lambda *a: "gs://synthetic/evidence",
        ),
        prefix="synthetic.dev",
        register_games=lambda games: registered.extend(games),
        mappings=lambda *a: [],
    )
    monkeypatch.setattr(
        worker,
        "capture_json",
        lambda url, *a: {"results": []} if "pbpstats" in url else schedule,
    )
    monkeypatch.setattr(worker, "archive_sources", lambda *a: "gs://synthetic/evidence")
    result = worker.discover(
        tmp_path, SEASON, DAY, DAY, store, None, as_of=datetime.fromisoformat(clock)
    )
    assert len(result) == selected and len(registered) == 1
    report = json.loads((tmp_path / "discovery.json").read_text())
    assert report["final_games"] == 1 and report["not_yet_due"] == 1 - selected
    assert report["deferred_by_limit"] == 0
    if not selected:
        assert report["publication_waits"][0]["expected_on"] == "2025-10-23"
