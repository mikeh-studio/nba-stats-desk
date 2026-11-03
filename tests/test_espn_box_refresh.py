import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import espn_box_refresh as worker
from scripts import weekly_refresh as weekly
from scripts.espn_sources import scoreboard
from tests.support.espn import DAY, SEASON, fixtures


def test_replay_window_includes_missed_dates_and_uses_eastern_yesterday():
    first, last = worker.dates_for_refresh(
        date(2026, 10, 20), "2026-10-24", datetime(2026, 10, 29, 2, tzinfo=timezone.utc)
    )
    assert (first, last) == (date(2026, 10, 20), date(2026, 10, 27))
    with pytest.raises(ValueError, match="31 dates"):
        worker.dates_for_refresh(
            date(2026, 7, 1), None, datetime(2026, 10, 28, tzinfo=timezone.utc)
        )


def fake_runtime(monkeypatch, tmp_path, payload, known=None):
    events = []
    cloud = SimpleNamespace()

    class Store:
        prefix = "example-project." + worker.DATASET

        def __init__(self, c):
            self.cloud = c

        def setup(self):
            pass

        def coverage(self, s):
            return known or []

        def register_games(self, g):
            events.append(("discovery", len(g)))

        def publish(self, c, e, o):
            events.append(("publish", c["layer"]))
            return {"layer": c["layer"], "status": "published"}

        def failure(self, g, layer, error, e, o):
            events.append(("failed", layer))
            return {"layer": layer, "status": "rejected"}

        def remember_players(self, p, e):
            events.append(("mappings", len(p)))
            return {"status": "published"}

    monkeypatch.setattr(worker, "CloudStore", lambda *a, **k: cloud)
    monkeypatch.setattr(worker, "ESPNLayerStore", Store)
    monkeypatch.setattr(
        worker, "QueryCheckpoint", lambda *a: SimpleNamespace(recover=lambda: None)
    )
    monkeypatch.setattr(
        worker.identity,
        "fetch_catalog",
        lambda *a: (
            [
                {"id": "10", "full_name": "Avery Able"},
                {"id": "11", "full_name": "Blake Baker"},
            ],
            {"method": "exact_unique_name", "sha256": "synthetic"},
        ),
    )
    monkeypatch.setattr(worker, "archive_sources", lambda *a: "gs://synthetic/evidence")
    schedule, _ = fixtures()

    def capture(url, directory, session):
        directory.mkdir(parents=True)
        (directory / "attempt.json").write_text(
            json.dumps({"finished_at": "2026-10-28T12:00:00+00:00"})
        )

    monkeypatch.setattr(worker, "capture_json", capture)
    monkeypatch.setattr(
        worker,
        "read_capture",
        lambda directory, url: schedule if "/scoreboard?" in url else payload,
    )
    return events


def test_box_refresh_never_publishes_pbp_or_requires_pbp(tmp_path, monkeypatch):
    _, payload = fixtures()
    payload.pop("plays")
    events = fake_runtime(monkeypatch, tmp_path, payload)
    r = worker.run(
        {"project": "example-project", "bucket": "synthetic", "season": SEASON},
        DAY,
        DAY,
        tmp_path / "out",
        session=object(),
    )
    assert r["provider"] == "espn" and r["status"] == "success"
    assert [x for x in events if x[0] == "publish"] == [
        ("publish", "result"),
        ("publish", "box"),
    ]
    assert r["identity_unresolved"] >= 1
    assert r["coverage_checks"]["player_checks"]["unresolved_rows"] >= 1
    assert r["coverage_checks"]["status"] == "blocked"
    assert (tmp_path / "out/coverage.json").exists()


def test_invalid_box_records_failure_and_never_publishes_it(tmp_path, monkeypatch):
    _, payload = fixtures()
    payload["boxscore"]["players"][0]["statistics"][0]["athletes"][0]["stats"][1] = (
        "99-1"
    )
    events = fake_runtime(monkeypatch, tmp_path, payload)
    with pytest.raises(ValueError):
        worker.run(
            {"project": "example-project", "bucket": "synthetic", "season": SEASON},
            DAY,
            DAY,
            tmp_path / "out",
            session=object(),
        )
    assert ("publish", "box") not in events and ("failed", "box") in events
    assert json.loads((tmp_path / "out/result.json").read_text())["status"] == "failed"


def test_espn_weekly_command_is_explicit_and_does_not_invoke_airflow(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "season.yml").write_text('current_season: "2026-27"\n')
    config = dict(
        project="example-project",
        bucket="synthetic-bucket",
        repo=str(repo),
        airflow="/usr/bin/airflow",
        state_dir=str(tmp_path / "state"),
        timezone="America/Los_Angeles",
        weekday=6,
        hour=8,
        season="2026-27",
        source="espn",
        first_date="2026-10-20",
    )
    calls = []
    monkeypatch.setattr(weekly, "command", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(
        weekly,
        "warehouse_scope",
        lambda _: {
            "groups": [
                {"season": "2026-27", "row_count": 100, "data_through": "2026-10-27"}
            ]
        },
    )
    state = weekly.refresh(config, now=datetime(2026, 10, 28, tzinfo=timezone.utc))
    assert state["status"] == "success"
    assert len(calls) == 2 and calls[0][1].endswith("espn_box_refresh.py")
    assert calls[1][1].endswith("espn_game_mapping_weekly.py")
    saved = json.loads(
        next((Path(config["state_dir"]) / "runs").glob("*.json")).read_text()
    )
    assert saved["stages"][0]["status"] == "success"
    assert saved["stages"][1]["status"] == "degraded"
    assert state["game_mapping"]["status"] == "degraded"
    assert "--first" in calls[0] and "2026-10-20" in calls[0]


def test_fresh_validated_unchanged_healthy_boxes_reuse_facts(tmp_path, monkeypatch):
    from scripts.espn_corrections import normalize, validate

    schedule, payload = fixtures()
    game = scoreboard(schedule, SEASON, DAY)[0]
    known = [
        dict(
            source_game_id=game["source_game_id"],
            game_date=DAY,
            result_status="accepted",
            box_status="accepted",
            result_revision=validate(normalize(payload, game, "result")),
            box_revision=validate(normalize(payload, game, "box")),
        )
    ]
    events = fake_runtime(monkeypatch, tmp_path, payload, known)
    r = worker.run(
        {"project": "example-project", "bucket": "synthetic", "season": SEASON},
        DAY,
        DAY,
        tmp_path / "out",
        session=object(),
    )
    assert not [x for x in events if x[0] == "publish"]
    assert all(layer["status"] == "validated_noop" for layer in r["games"][0]["layers"])
    assert all(layer["evidence_uri"] for layer in r["games"][0]["layers"])


def test_failed_health_is_revalidated_through_the_publisher(tmp_path, monkeypatch):
    from scripts.espn_corrections import normalize, validate

    schedule, payload = fixtures()
    game = scoreboard(schedule, SEASON, DAY)[0]
    known = [
        dict(
            source_game_id=game["source_game_id"],
            game_date=DAY,
            result_status="accepted",
            box_status="retained_after_failure",
            result_revision=validate(normalize(payload, game, "result")),
            box_revision=validate(normalize(payload, game, "box")),
        )
    ]
    events = fake_runtime(monkeypatch, tmp_path, payload, known)
    worker.run(
        {"project": "example-project", "bucket": "synthetic", "season": SEASON},
        DAY,
        DAY,
        tmp_path / "out",
        session=object(),
    )
    assert ("publish", "box") in events


def test_discovery_cannot_drop_an_existing_observed_final(tmp_path, monkeypatch):
    _, payload = fixtures()
    events = fake_runtime(
        monkeypatch, tmp_path, payload, [dict(source_game_id="9999999", game_date=DAY)]
    )
    with pytest.raises(ValueError, match="lost previously"):
        worker.run(
            {"project": "example-project", "bucket": "synthetic", "season": SEASON},
            DAY,
            DAY,
            tmp_path / "out",
            session=object(),
        )
    assert not [x for x in events if x[0] == "publish"]
    assert (
        json.loads((tmp_path / "out/result.json").read_text())["discovery"][0]["status"]
        == "failed"
    )


def test_cli_imports_work_from_outside_the_checkout(tmp_path):
    import subprocess
    import sys

    script = __import__("pathlib").Path(weekly.__file__)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy;runpy.run_path("
            + repr(str(script))
            + ");from scripts.espn_box_refresh import dates_for_refresh",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_retryable_capture_failure_keeps_evidence_and_retries_bounded(
    tmp_path, monkeypatch
):
    calls = []
    archived = []

    def capture(url, path, session):
        calls.append(path.name)
        if len(calls) < 3:
            raise worker.SourceUnavailable("synthetic empty body")

    monkeypatch.setattr(worker, "capture_json", capture)
    monkeypatch.setattr(worker, "read_capture", lambda *a: {"events": []})
    monkeypatch.setattr(
        worker, "archive_sources", lambda c, path: archived.append(path.name)
    )
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    retries = []
    payload, path = worker.captured_json(
        "https://example.test", tmp_path, object(), object(), retries
    )
    assert payload == {"events": []} and path.name == "attempt-3"
    assert archived == ["attempt-1", "attempt-2"] and len(retries) == 2


def test_repeated_capture_failure_never_becomes_an_empty_success(tmp_path, monkeypatch):
    calls = []

    def fail(*a):
        calls.append(1)
        raise worker.SourceUnavailable("synthetic")

    monkeypatch.setattr(worker, "capture_json", fail)
    monkeypatch.setattr(worker, "archive_sources", lambda *a: None)
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    with pytest.raises(worker.SourceUnavailable):
        worker.captured_json("https://example.test", tmp_path, object(), object(), [])
    assert len(calls) == 3
