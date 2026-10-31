"""Local monitoring never launches a writer or queries cloud services."""

import asyncio
import fcntl
import json
import plistlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from app.jobs import create_jobs_app, scheduler_state, snapshot
from scripts import weekly_refresh as weekly

UTC = timezone.utc
NOW = datetime(2026, 10, 28, 19, tzinfo=UTC)
RUN_ID = "a" * 32


def test_mapping_projection_keeps_conflicts_and_excludes_raw_evidence(directory):
    save(
        directory,
        "status.json",
        {
            "game_mapping": {
                "status": "degraded",
                "games": 56,
                "resolved": 56,
                "discrepancy_count": 1,
                "evidence_uri": "gs://private",
                "discrepancies": [
                    {
                        "source_game_id": "1234567",
                        "nba_game_id": "0022500001",
                        "date": "2025-10-22",
                        "home": "NYK",
                        "away": "BOS",
                        "home_score": 100,
                        "away_score": 99,
                        "provider_home_score": 100,
                        "provider_away_score": 97,
                        "identity_verified_separately": True,
                        "reason": "score_conflict",
                        "detail": "private",
                    }
                ],
            }
        },
    )
    mapping = snapshot(directory, scheduler(), NOW)["job"]["game_mapping"]
    assert mapping["resolved"] == 56 and mapping["discrepancy_count"] == 1
    assert mapping["discrepancies"][0]["provider_away_score"] == 97
    assert mapping["discrepancies"][0]["identity_verified_separately"]
    assert "private" not in json.dumps(mapping)


def test_coverage_projection_keeps_checks_and_excludes_private_artifacts(directory):
    save(
        directory,
        "status.json",
        {
            "coverage_checks": {
                "status": "blocked",
                "dates_expected": 8,
                "dates_checked": 7,
                "expected_final_games": 56,
                "accepted_games": 55,
                "missing_games": ["401909088", "/private/path"],
                "missing_dates": ["2026-10-22"],
                "metric_checks": {
                    "pts": {"coverage_ready": False, "missing_input_rows": 1}
                },
                "identities": {"private": "internal"},
                "raw_capture_path": "/private/path",
            }
        },
    )
    checks = snapshot(directory, scheduler(), NOW)["job"]["coverage_checks"]
    assert checks["missing_games"] == ["401909088"]
    assert checks["dates_checked"] == 7
    assert checks["metric_checks"]["pts"]["coverage_ready"] is False
    assert "private" not in json.dumps(checks)


def save(directory, name, value):
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def scheduler(loaded=False):
    return lambda _: {
        "installed": loaded,
        "loaded": loaded,
        "label": "Enabled" if loaded else "Not enabled",
    }


@pytest.fixture
def directory(tmp_path):
    save(
        tmp_path,
        "config.json",
        {
            "timezone": "America/Los_Angeles",
            "weekday": 6,
            "hour": 8,
            "season": "2026-27",
        },
    )
    return tmp_path


def test_uninstalled_schedule_handles_dst_without_claiming_a_queued_run(directory):
    save(
        directory,
        "status.json",
        {
            "status": "blocked",
            "reason": "season_rollover_required",
            "observed_scope": {
                "groups": [
                    {
                        "season": "2025-26",
                        "row_count": 28572,
                        "data_through": "2026-06-13",
                    }
                ]
            },
        },
    )
    job = snapshot(directory, scheduler(), NOW)["job"]
    assert job["schedule"]["next_weekly_at"] == "2026-11-01T16:00:00+00:00"
    assert job["schedule"]["next_scheduled_run"] is None
    assert job["freshness"]["data_through"] is None
    assert job["freshness"]["observed"][0]["season"] == "2025-26"
    assert job["last_duration_seconds"] is None
    assert job["history"] == []


@pytest.mark.parametrize(
    "attempts,last_attempt,expected",
    [
        (1, "2026-10-28T18:00:00+00:00", "2026-10-29T18:00:00+00:00"),
        (1, "2026-10-27T18:00:00+00:00", NOW.isoformat()),
        (3, "2026-10-27T18:00:00+00:00", None),
    ],
)
def test_retry_eligibility_and_exhaustion(directory, attempts, last_attempt, expected):
    save(
        directory,
        "status.json",
        {
            "attempt_slot": "2026-10-25T15:00:00+00:00",
            "attempt_count": attempts,
            "last_attempt_at": last_attempt,
        },
    )
    plan = snapshot(directory, scheduler(True), NOW)["job"]["schedule"]
    assert plan["retry_at"] == expected
    assert plan["next_scheduled_run"] == (expected or plan["next_weekly_at"])


def test_interrupted_record_is_not_permanently_running(directory):
    save(
        directory,
        f"runs/{RUN_ID}.json",
        {
            "id": RUN_ID,
            "status": "running",
            "started_at": NOW.isoformat(),
            "stages": None,
        },
    )
    job = snapshot(directory, scheduler(), NOW)["job"]
    assert job["status"] == "interrupted"
    assert job["reason"] == "worker_not_running"
    with (directory / "refresh.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        job = snapshot(directory, scheduler(True), NOW)["job"]
        assert job["status"] == "running"
        assert job["schedule"]["retry_at"] is None


def test_corrupt_records_report_issues_and_keep_valid_history(directory):
    save(
        directory,
        f"runs/{RUN_ID}.json",
        {"id": RUN_ID, "status": "success", "duration_seconds": 10},
    )
    (directory / "runs/broken.json").write_text("{")
    (directory / "refresh.lock").symlink_to(directory / "config.json")
    job = snapshot(directory, scheduler(), NOW)["job"]
    assert len(job["history"]) == 1
    assert len(job["issues"]) == 2


@pytest.mark.parametrize(
    "loaded,disabled,expected",
    [(True, False, True), (True, True, False), (False, False, False)],
)
def test_scheduler_checks_loaded_label_and_disabled_override(
    directory, monkeypatch, loaded, disabled, expected
):
    monkeypatch.setattr(Path, "home", lambda: directory)
    agent = directory / "Library/LaunchAgents/com.nba-gcp.weekly-refresh.plist"
    agent.parent.mkdir(parents=True)
    agent.write_bytes(
        plistlib.dumps(
            {"ProgramArguments": ["python", "--config", str(directory / "config.json")]}
        )
    )

    def launchctl(args, **kwargs):
        output = (
            ("- 0 com.nba-gcp.weekly-refresh" if loaded else "")
            if args[1] == "list"
            else ('"com.nba-gcp.weekly-refresh" => true' if disabled else "")
        )
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(subprocess, "run", launchctl)
    assert scheduler_state(directory)["loaded"] is expected


def test_install_for_different_state_directory_is_not_this_job(directory, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: directory)
    agent = directory / "Library/LaunchAgents/com.nba-gcp.weekly-refresh.plist"
    agent.parent.mkdir(parents=True)
    agent.write_bytes(
        plistlib.dumps(
            {"ProgramArguments": ["python", "--config", "/other/config.json"]}
        )
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: pytest.fail("Queried another job")
    )
    assert scheduler_state(directory)["installed"] is False


def request(app, path="/api/jobs", headers=None, peer="127.0.0.1", host="localhost"):
    async def fetch():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(peer, 12345)),
            base_url=f"http://{host}",
        ) as client:
            return await client.get(path, headers=headers)

    return asyncio.run(fetch())


def test_api_reads_only_local_records_and_serves_all_assets(directory, monkeypatch):
    monkeypatch.setattr(
        weekly, "warehouse_scope", lambda _: pytest.fail("Cloud query from monitor")
    )
    app = create_jobs_app(directory, scheduler=scheduler())
    response = request(app)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["job"]["status"] == "not_run"
    for path in (
        "/jobs",
        "/assets/jobs.js",
        "/assets/jobs.css",
        "/fonts/barlow.css",
        "/fonts/barlow-400.ttf",
    ):
        assert request(app, path).status_code == 200


@pytest.mark.parametrize(
    "options",
    [
        {"peer": "192.0.2.2"},
        {"host": "example.com"},
        {"headers": {"Origin": "https://example.com"}},
        {"headers": {"X-Forwarded-For": "127.0.0.1"}},
        {"headers": {"Sec-Fetch-Site": "cross-site"}},
    ],
)
def test_monitor_requires_direct_local_access(directory, options):
    assert request(create_jobs_app(directory), **options).status_code == 403


def test_log_is_bounded_redacted_and_requires_exact_record_identity(directory):
    save(
        directory,
        f"runs/{RUN_ID}.json",
        {"id": RUN_ID, "log_name": f"run-{RUN_ID}.log"},
    )
    log = directory / f"run-{RUN_ID}.log"
    log.write_text("x" * 70000 + "\nAuthorization: Bearer synthetic-private-token\n")
    app = create_jobs_app(directory, scheduler=scheduler())
    response = request(app, f"/api/jobs/runs/{RUN_ID}/log")
    assert response.status_code == 200
    assert response.json()["truncated"] is True
    assert "synthetic-private-token" not in response.json()["text"]
    log.unlink()
    log.symlink_to(directory / "config.json")
    assert request(app, f"/api/jobs/runs/{RUN_ID}/log").status_code == 404
    assert request(app, "/api/jobs/runs/not-a-run/log").status_code == 404


def test_freshness_is_age_of_target_data_not_age_of_success(directory):
    save(
        directory,
        "status.json",
        {
            "status": "success",
            "last_success_at": NOW.isoformat(),
            "observed_scope": {
                "groups": [{"season": "2026-27", "data_through": "2026-10-01"}]
            },
        },
    )
    job = snapshot(directory, scheduler(), NOW)["job"]
    assert job["freshness"]["age_days"] == 27
    assert job["freshness"]["state"] == "stale"


def test_preflight_creates_measured_history_without_counting_a_refresh(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "season.yml").write_text('current_season: "2026-27"\n')
    config = dict(
        project="example-project",
        bucket="example-bucket",
        repo=str(repo),
        airflow="/usr/bin/airflow",
        state_dir=str(tmp_path / "state"),
        season="2026-27",
        timezone="America/Los_Angeles",
        weekday=6,
        hour=8,
    )
    monkeypatch.setattr(
        weekly,
        "warehouse_scope",
        lambda _: weekly.assess_rows(
            [dict(season="2025-26", row_count=100, data_through="2026-06-13")],
            "2026-27",
        ),
    )
    monkeypatch.setattr(
        weekly, "command", lambda *a, **kw: pytest.fail("Preflight started ingestion")
    )
    result = weekly.refresh(config, check_only=True)
    assert result["status"] == "blocked"
    assert "attempt_count" not in result
    assert result["scope_observed_at"]
    records = list((tmp_path / "state/runs").glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["kind"] == "preflight"
    assert record["duration_seconds"] >= 0
    assert record["finished_at"] >= record["started_at"]
    assert record["observed_scope"]["groups"][0]["season"] == "2025-26"
    assert record["stages"] == []


def test_multiple_jobs_keep_sources_histories_and_logs_separate(directory, tmp_path):
    second = tmp_path / "espn"
    save(
        second,
        "config.json",
        {
            "source": "espn",
            "season": "2026-27",
            "timezone": "America/Los_Angeles",
            "weekday": 6,
            "hour": 8,
        },
    )
    save(
        second,
        "status.json",
        {
            "observed_scope": {
                "groups": [
                    {
                        "season": "2026-27",
                        "row_count": 10,
                        "data_through": "2026-10-27",
                        "unmapped_rows": 2,
                    }
                ]
            }
        },
    )
    save(
        second,
        f"runs/{RUN_ID}.json",
        {"id": RUN_ID, "source": "espn", "log_name": f"run-{RUN_ID}.log"},
    )
    (second / f"run-{RUN_ID}.log").write_text("synthetic ESPN log")
    app = create_jobs_app(second, scheduler=scheduler(), also_state_dirs=[directory])
    payload = request(app).json()
    assert [j["source"] for j in payload["jobs"]] == ["espn", "nba"]
    assert payload["job"]["name"] == "Weekly ESPN box-score refresh"
    assert "2 ESPN player appearances" in payload["job"]["issues"][0]
    assert (
        request(app, f"/api/jobs/runs/{RUN_ID}/log").json()["text"]
        == "synthetic ESPN log"
    )
