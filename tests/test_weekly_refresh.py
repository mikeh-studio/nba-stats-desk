from __future__ import annotations

import fcntl
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from scripts import install_weekly_refresh as installer
from scripts import weekly_refresh as weekly
from scripts.install_weekly_refresh import launch_agent

UTC = timezone.utc


@pytest.fixture
def config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "season.yml").write_text('current_season: "2026-27"\n')
    return dict(
        project="example-project",
        bucket="example-bucket",
        repo=str(repo),
        airflow="/usr/local/bin/airflow",
        state_dir=str(tmp_path / "state"),
        timezone="America/Los_Angeles",
        weekday=6,
        hour=8,
        season="2026-27",
    )


def test_sunday_slot_handles_dst_and_before_schedule():
    # DST ends November 1: eight Pacific is 16:00 UTC, not 15:00 UTC.
    assert weekly.slot(
        datetime(2026, 11, 1, 16, tzinfo=UTC), "America/Los_Angeles"
    ) == datetime(2026, 11, 1, 16, tzinfo=UTC)
    assert weekly.slot(
        datetime(2026, 11, 1, 15, 59, tzinfo=UTC), "America/Los_Angeles"
    ) == datetime(2026, 10, 25, 15, tzinfo=UTC)


def test_missed_weeks_coalesce_and_success_prevents_duplicate_fetch():
    now = datetime(2026, 11, 18, 10, tzinfo=UTC)
    current = weekly.slot(now, "America/Los_Angeles")
    assert current == datetime(2026, 11, 15, 16, tzinfo=UTC)
    assert weekly.due({"last_success_slot": "2026-10-25T15:00:00+00:00"}, current, now)
    assert not weekly.due({"last_success_slot": current.isoformat()}, current, now)


def test_retries_wait_a_day_and_stop_after_three_attempts():
    now = datetime(2026, 11, 2, 16, tzinfo=UTC)
    current = weekly.slot(now, "America/Los_Angeles")
    state = dict(
        attempt_slot=current.isoformat(),
        attempt_count=1,
        last_attempt_at=(now - timedelta(hours=23)).isoformat(),
    )
    assert not weekly.due(state, current, now)
    state["last_attempt_at"] = (now - timedelta(days=1)).isoformat()
    assert weekly.due(state, current, now)
    state["attempt_count"] = 3
    assert not weekly.due(state, current, now)
    assert weekly.due(state, current + timedelta(days=7), now + timedelta(days=7))


def test_rollover_block_prevents_any_pipeline_commands(config, monkeypatch):
    commands = []
    monkeypatch.setattr(
        weekly,
        "warehouse_scope",
        lambda _: weekly.assess_rows(
            [dict(season="2025-26", row_count=100)], "2026-27"
        ),
    )
    monkeypatch.setattr(weekly, "command", lambda *a, **kw: commands.append(a))
    result = weekly.refresh(config)
    assert result["status"] == "blocked"
    assert result["reason"] == "season_rollover_required"
    assert result["attempt_count"] == 1
    assert "last_success_slot" not in result
    assert commands == []
    assert weekly.refresh(config)["action"] == "not_due"


def test_failed_run_retains_previous_success_and_never_marks_week_done(
    config, monkeypatch
):
    directory = Path(config["state_dir"])
    directory.mkdir()
    previous = "2026-10-18T15:00:00+00:00"
    (directory / "status.json").write_text(json.dumps({"last_success_slot": previous}))
    monkeypatch.setattr(weekly, "warehouse_scope", lambda _: {"groups": []})

    def fail(*args, **kwargs):
        raise RuntimeError("pipeline_command_failed")

    monkeypatch.setattr(weekly, "command", fail)
    result = weekly.refresh(config, now=datetime(2026, 10, 28, tzinfo=UTC))
    assert result["status"] == "failed"
    assert result["reason"] == "pipeline_command_failed"
    assert result["last_success_slot"] == previous
    record = json.loads(next((directory / "runs").glob("*.json")).read_text())
    assert record["status"] == "failed"
    assert record["stages"][0]["status"] == "failed"
    assert record["stages"][0]["duration_seconds"] >= 0
    assert record["log_name"] == f"run-{record['id']}.log"


def test_success_requires_fresh_published_data(config, monkeypatch):
    now = datetime(2026, 10, 28, 12, tzinfo=UTC)
    calls = []
    monkeypatch.setattr(weekly, "command", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(
        weekly,
        "warehouse_scope",
        lambda _: {
            "groups": [dict(season="2026-27", row_count=10, data_through="2026-10-27")]
        },
    )
    result = weekly.refresh(config, now=now)
    assert result["status"] == "success"
    assert result["data_through"] == "2026-10-27"
    assert len(calls) == 2
    records = list((Path(config["state_dir"]) / "runs").glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["status"] == "success"
    assert [s["status"] for s in record["stages"]] == ["success", "success"]
    weekly.refresh(config, now=now)
    assert len(calls) == 2


def test_empty_or_stale_output_is_failed_even_with_successful_exit(config, monkeypatch):
    monkeypatch.setattr(weekly, "command", lambda *a, **kw: None)
    monkeypatch.setattr(weekly, "warehouse_scope", lambda _: {"groups": []})
    assert weekly.refresh(config)["reason"] == "stale_or_empty_publication"


def test_overlap_returns_without_touching_network(config, monkeypatch):
    directory = Path(config["state_dir"])
    directory.mkdir()
    with (directory / "refresh.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(
            weekly, "warehouse_scope", lambda _: pytest.fail("overlapping read")
        )
        assert weekly.refresh(config) == {"status": "already_running"}


def test_explicit_runtime_excludes_inherited_settings_and_model_secrets(
    config, monkeypatch
):
    monkeypatch.setenv("AIRFLOW__CORE__DAGS_FOLDER", "/wrong/repo")
    monkeypatch.setenv("BQ_DATASET", "wrong_dataset")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-value")
    env = weekly.environment(config)
    assert "OPENAI_API_KEY" not in env and "BQ_DATASET" not in env
    assert env["AIRFLOW__CORE__DAGS_FOLDER"] == config["repo"] + "/dags"
    assert env["PYTHON_DOTENV_DISABLED"] == "1"
    assert env["NBA_REPLAY_DAYS"] == "14"


def test_launch_agent_recovers_login_and_has_one_weekly_slot(config, tmp_path):
    agent = launch_agent(config, tmp_path / "config.json", "/usr/bin/python3")
    assert agent["StartCalendarInterval"] == {"Weekday": 0, "Hour": 8, "Minute": 0}
    assert agent["RunAtLoad"] and agent["StartInterval"] == 3600
    assert "KeepAlive" not in agent
    assert "make" not in agent["ProgramArguments"]


def test_timeout_terminates_and_reaps_entire_command_group(
    config, tmp_path, monkeypatch
):
    class Process:
        pid = 12345
        waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("synthetic", timeout)
            return -15

    process = Process()
    signals = []
    monkeypatch.setattr(weekly.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(weekly.os, "killpg", lambda *args: signals.append(args))
    with pytest.raises(RuntimeError, match="pipeline_timeout"):
        weekly.command(["synthetic"], config=config, log=tmp_path / "log", timeout=1)
    assert len(signals) == 1 and process.waits == 2


def test_installation_cannot_load_agent_when_preflight_is_blocked(
    config, tmp_path, monkeypatch
):
    monkeypatch.setattr(installer, "ROOT", Path(config["repo"]))
    monkeypatch.setattr(
        installer,
        "refresh",
        lambda *a, **kw: {"status": "blocked", "reason": "season_rollover_required"},
    )
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *a, **kw: pytest.fail("blocked installation called launchctl"),
    )
    monkeypatch.setattr(
        installer.sys,
        "argv",
        [
            "install",
            "--project",
            config["project"],
            "--bucket",
            config["bucket"],
            "--season",
            config["season"],
            "--airflow",
            config["airflow"],
            "--state-dir",
            config["state_dir"],
            "--install",
        ],
    )
    assert installer.main() == 2
    assert (Path(config["state_dir"]) / "com.nba-gcp.weekly-refresh.plist").exists()


def test_espn_schedule_requires_an_initial_successful_refresh(config, monkeypatch):
    monkeypatch.setattr(installer, "ROOT", Path(config["repo"]))
    monkeypatch.setattr(
        installer, "refresh", lambda *a, **kw: {"status": "preflight_passed"}
    )
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *a, **kw: pytest.fail("Activated before initial success"),
    )
    monkeypatch.setattr(
        installer.sys,
        "argv",
        [
            "install",
            "--project",
            config["project"],
            "--bucket",
            config["bucket"],
            "--season",
            config["season"],
            "--airflow",
            config["airflow"],
            "--state-dir",
            config["state_dir"],
            "--source",
            "espn",
            "--first-date",
            "2026-10-20",
            "--install",
        ],
    )
    assert installer.main() == 2
