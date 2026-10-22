from __future__ import annotations

import copy
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import gamebook_ingestion as worker
from scripts import gamebook_warehouse_discovery as warehouse
from scripts.fetch_gamebooks import source_url

SEASON = "2025-26"
NOW = datetime(2025, 10, 23, 12, tzinfo=timezone.utc)
START, END = date(2025, 7, 1), NOW.date()
ROWS = [
    {
        "game_id": "0022500003",
        "game_date": "2025-10-22",
        "matchup": "CLE @ NYK",
        "wl": "L",
        "player_rows": 10,
    },
    {
        "game_id": "0022500003",
        "game_date": "2025-10-22",
        "matchup": "NYK vs. CLE",
        "wl": "W",
        "player_rows": 11,
    },
]
RUN = {
    "dag_run_id": "synthetic_run",
    "finished_at_utc": NOW - timedelta(hours=1),
    "status": "success",
    "details": '{"stats_status":"success"}',
    "watermark_after": "2025-10-22",
}


def test_completed_pair_preserves_ids_and_builds_exact_pdf_url():
    games, errors = warehouse.completed_games(ROWS, SEASON, START, END)
    assert errors == [] and len(games) == 1
    assert games[0]["game_id"] == "0022500003"
    assert games[0]["home"] == "NYK" and games[0]["away_name"] == "Cleveland Cavaliers"
    assert (
        source_url(games[0])
        == "https://statsdmz.nba.com/pdfs/20251022/20251022_CLENYK_book.pdf"
    )
    assert warehouse.completed_games(ROWS * 2, SEASON, START, END) == (games, [])


@pytest.mark.parametrize(
    "defect",
    [
        "one_side",
        "no_result",
        "two_winners",
        "nonreciprocal",
        "two_homes",
        "date_conflict",
        "wrong_year",
        "playoff",
        "numeric_id",
        "unknown_team",
        "bad_matchup",
        "extra_outcome",
    ],
)
def test_incomplete_or_conflicting_game_never_becomes_completed(defect):
    rows = copy.deepcopy(ROWS)
    if defect == "one_side":
        rows = rows[:1]
    elif defect == "no_result":
        rows[0]["wl"] = None
    elif defect == "two_winners":
        rows[0]["wl"] = "W"
    elif defect == "nonreciprocal":
        rows[0]["matchup"] = "CLE @ BOS"
    elif defect == "two_homes":
        rows[0]["matchup"] = "CLE vs. NYK"
    elif defect == "date_conflict":
        rows[0]["game_date"] = "2025-10-23"
    elif defect == "wrong_year":
        for row in rows:
            row["game_id"] = "0022400003"
    elif defect == "playoff":
        for row in rows:
            row["game_id"] = "0042500003"
    elif defect == "numeric_id":
        for row in rows:
            row["game_id"] = 22500003
    elif defect == "unknown_team":
        rows[0]["matchup"] = "ZZZ @ NYK"
    elif defect == "bad_matchup":
        rows[0]["matchup"] = "CLE @ NYK;"
    else:
        rows.append({**rows[0], "wl": "W"})
    games, errors = warehouse.completed_games(rows, SEASON, START, END)
    assert not games and errors


def test_limit_cannot_silently_drop_source_games():
    with pytest.raises(ValueError, match="truncation"):
        warehouse.completed_games(
            ROWS * (warehouse.MAX_GROUPS // 2 + 1), SEASON, START, END
        )


@pytest.mark.parametrize(
    "defect", ["missing", "stale", "future", "failed", "bad_details", "stats_failed"]
)
def test_daily_freshness_rejects_stale_missing_or_failed_upstream(defect):
    run = dict(RUN)
    if defect == "stale":
        run["finished_at_utc"] = NOW - timedelta(hours=37)
    elif defect == "future":
        run["finished_at_utc"] = NOW + timedelta(minutes=1)
    elif defect == "failed":
        run["status"] = "failed"
    elif defect == "bad_details":
        run["details"] = "[]"
    elif defect == "stats_failed":
        run["details"] = '{"stats_status":"failed"}'
    with pytest.raises(ValueError):
        warehouse.check_freshness([] if defect == "missing" else [run], NOW)
    assert warehouse.check_freshness([RUN], NOW)["dag_run_id"] == "synthetic_run"


def test_backfill_must_be_explicit_bounded_and_does_not_change_daily_policy():
    assert warehouse.discovery_window(SEASON, NOW, "daily") == (START, END)
    with pytest.raises(ValueError, match="explicit"):
        warehouse.discovery_window(SEASON, NOW, "backfill")
    with pytest.raises(ValueError, match="31 days"):
        warehouse.discovery_window(SEASON, NOW, "backfill", "2025-08-01", "2025-10-01")
    with pytest.raises(ValueError, match="active calendar"):
        warehouse.discovery_window(SEASON, datetime(2026, 10, 22), "daily")
    with pytest.raises(ValueError, match="whole observed"):
        warehouse.discovery_window(SEASON, NOW, "daily", "2025-10-22", "2025-10-22")
    assert warehouse.discovery_window(
        SEASON, NOW, "backfill", "2025-10-22", "2025-10-22"
    ) == (date(2025, 10, 22), date(2025, 10, 22))


class Store:
    project = "example-project"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.jobs = []
        self.calls = []

    def parameter(self, *args):
        return args

    def query(self, sql, params):
        # This adapter must never mutate its source tables.
        assert sql.startswith("SELECT")
        assert "LIMIT" in sql
        self.calls.append((sql, params))
        self.jobs.append({"job_id": f"job-{len(self.jobs)}"})
        return next(self.responses)


def test_capture_is_read_only_bounded_and_archives_source_provenance(tmp_path):
    store = Store([[RUN], ROWS])
    games = warehouse.capture(
        store,
        SEASON,
        NOW,
        tmp_path / "capture",
        bronze_dataset="nba_bronze",
        metadata_dataset="nba_metadata",
    )
    assert len(games) == 1 and len(store.calls) == 2
    assert "season_type='Regular Season'" in store.calls[1][0]
    assert "@start" in store.calls[1][0] and "@end" in store.calls[1][0]
    info = json.loads((tmp_path / "capture/selection.json").read_text())
    assert info["status"] == "available" and info["query_job_ids"] == ["job-0", "job-1"]
    assert "not independent league-wide" in info["coverage_scope"]
    assert (tmp_path / "capture/game_contexts.json").exists()


def test_zero_games_requires_fresh_upstream_and_failure_retains_evidence(tmp_path):
    store = Store([[RUN], []])
    assert (
        warehouse.capture(
            store,
            SEASON,
            NOW,
            tmp_path / "empty",
            bronze_dataset="nba_bronze",
            metadata_dataset="nba_metadata",
        )
        == []
    )
    missing = Store([[]])
    with pytest.raises(ValueError, match="No upstream"):
        warehouse.capture(
            missing,
            SEASON,
            NOW,
            tmp_path / "missing",
            bronze_dataset="nba_bronze",
            metadata_dataset="nba_metadata",
        )
    assert len(missing.calls) == 1
    assert (
        json.loads((tmp_path / "missing/selection.json").read_text())["status"]
        == "failed"
    )


def test_backfill_failure_records_full_conflicting_input(tmp_path):
    store = Store([ROWS[:1]])
    with pytest.raises(ValueError, match="incomplete"):
        warehouse.capture(
            store,
            SEASON,
            NOW,
            tmp_path / "bad",
            bronze_dataset="nba_bronze",
            metadata_dataset="nba_metadata",
            mode="backfill",
            start_date="2025-10-22",
            end_date="2025-10-22",
        )
    errors = json.loads((tmp_path / "bad/rejected_contexts.json").read_text())
    assert errors[0]["records"] == ROWS[:1]
    assert not (tmp_path / "bad/manifest.json").exists()


@pytest.mark.parametrize(
    "dataset", ["bronze` WHERE TRUE;--", "project.dataset", "../nba_bronze"]
)
def test_source_identifiers_cannot_inject_sql(dataset):
    with pytest.raises(ValueError):
        warehouse.table_ref("example-project", dataset, "raw_game_logs")


def test_default_worker_discovery_uses_warehouse_without_http(monkeypatch, tmp_path):
    games, _ = warehouse.completed_games(ROWS, SEASON, START, END)
    monkeypatch.setattr(warehouse, "capture", lambda *args, **kwargs: games)
    monkeypatch.setattr(
        worker.sources,
        "discover",
        lambda *args: pytest.fail("schedule HTTP must not run"),
    )
    monkeypatch.setattr(
        worker, "archive_sources", lambda *args: "gs://synthetic/evidence"
    )
    monkeypatch.setattr(worker, "coverage", lambda *args: [])
    monkeypatch.setattr(worker, "register_completed", lambda *args: None)

    class Sink:
        def upload(self, *args):
            pass

    selected = worker.run_discovery(tmp_path, SEASON, NOW, Sink(), None)
    assert selected == games
    assert (
        json.loads((tmp_path / "discovery.json").read_text())["discovery_source"]
        == "warehouse"
    )


@pytest.mark.parametrize(
    "inside,missing", [(False, False), (True, False), (True, True)]
)
def test_backfill_membership_guard_is_limited_to_requested_dates(
    monkeypatch, tmp_path, inside, missing
):
    games, _ = warehouse.completed_games(ROWS, SEASON, START, END)
    monkeypatch.setattr(warehouse, "capture", lambda *args, **kwargs: games)
    monkeypatch.setattr(
        worker, "archive_sources", lambda *args: "gs://synthetic/evidence"
    )
    state = {
        "game_id": "0022500099" if missing or not inside else games[0]["game_id"],
        "game_date": "2025-10-22" if inside else "2025-10-21",
    }
    monkeypatch.setattr(worker, "coverage", lambda *args: [state])
    monkeypatch.setattr(worker, "register_completed", lambda *args: None)

    class Sink:
        def upload(self, *args):
            pass

    kwargs = {"mode": "backfill", "start_date": "2025-10-22", "end_date": "2025-10-22"}
    if inside and missing:
        with pytest.raises(ValueError, match="lost previously"):
            worker.run_discovery(tmp_path, SEASON, NOW, Sink(), None, **kwargs)
    else:
        assert (
            worker.run_discovery(tmp_path, SEASON, NOW, Sink(), None, **kwargs) == games
        )
