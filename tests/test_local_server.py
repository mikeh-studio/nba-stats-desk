"""Local compositions retain isolation, authority and launcher compatibility."""

import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app import local_server


@pytest.mark.parametrize(
    "script,required",
    [
        ("serve_jobs.py", "--also-state-dir"),
        ("serve_app_with_jobs.py", "--snapshot-root"),
        ("serve_weekly_candidate.py", "--authority"),
    ],
)
def test_legacy_launcher_help_is_safe_and_preserves_options(script, required):
    result = subprocess.run(
        [sys.executable, "scripts/" + script, "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert required in result.stdout


@pytest.mark.parametrize("directory", ["tests", "tests/manual"])
@pytest.mark.parametrize(
    "runner",
    [
        "espn_cloud_scenarios",
        "espn_identity_cloud_scenarios",
        "espn_layer_cloud_scenarios",
        "espn_only_cloud_scenarios",
        "gamebook_automation_cloud_scenarios",
        "gamebook_cloud_scenarios",
    ],
)
def test_manual_cloud_runner_help_keeps_imports_without_running(directory, runner):
    result = subprocess.run(
        [sys.executable, f"{directory}/{runner}.py", "--help"],
        env={**os.environ, "PYTHON_DOTENV_DISABLED": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "--output" in result.stdout


def test_jobs_mode_does_not_import_normal_app(monkeypatch, tmp_path):
    import app.jobs

    create = Mock(return_value=object())
    monkeypatch.setattr(app.jobs, "create_jobs_app", create)
    monkeypatch.setitem(sys.modules, "app.main", None)
    args = local_server.parser().parse_args(
        [
            "jobs",
            "--state-dir",
            str(tmp_path),
            "--also-state-dir",
            str(tmp_path / "other"),
        ]
    )
    assert local_server.build_application(args) is create.return_value
    assert create.call_args.kwargs["also_state_dirs"] == [tmp_path / "other"]


def test_app_composition_preserves_snapshot_and_additional_jobs(monkeypatch, tmp_path):
    import app.jobs
    import app.serving.local_weekly

    application = SimpleNamespace(state=SimpleNamespace(), mount=Mock())
    monkeypatch.setitem(sys.modules, "app.main", SimpleNamespace(app=application))
    create = Mock(return_value=object())
    weekly = Mock(return_value=object())
    monkeypatch.setattr(app.jobs, "create_jobs_app", create)
    monkeypatch.setattr(app.serving.local_weekly, "LocalWeeklyPTS", weekly)
    args = local_server.parser().parse_args(
        [
            "app",
            "--state-dir",
            str(tmp_path),
            "--snapshot-root",
            str(tmp_path / "facts"),
            "--also-state-dir",
            str(tmp_path / "other"),
        ]
    )
    assert local_server.build_application(args) is application
    application.mount.assert_called_once_with("/jobs-monitor", create.return_value)
    assert application.state.weekly_pts is weekly.return_value
    assert application.state.jobs_monitor_enabled
    assert create.call_args.kwargs["snapshot_root"] == tmp_path / "facts"
    assert create.call_args.kwargs["also_state_dirs"] == [tmp_path / "other"]


def test_weekly_composition_requires_authority_and_current_code(monkeypatch, tmp_path):
    import app.serving.code_identity
    import app.serving.weekly_authority

    authority = Mock()
    monkeypatch.setattr(app.serving.weekly_authority, "WeeklyAuthority", authority)
    monkeypatch.setattr(
        app.serving.code_identity, "app_code_id", lambda root: "code-id"
    )
    args = local_server.parser().parse_args(
        [
            "weekly",
            "--data-root",
            str(tmp_path),
            "--authority",
            str(tmp_path / "authority"),
        ]
    )
    assert (
        local_server.build_application(args)
        is authority.return_value.application.return_value
    )
    authority.assert_called_once_with(
        tmp_path / "authority", expected_code_id="code-id"
    )
    assert authority.return_value.application.call_args.kwargs["jobs_app"] is None
    with pytest.raises(SystemExit):
        local_server.parser().parse_args(["weekly", "--data-root", str(tmp_path)])


def test_server_binds_loopback_without_proxy_or_dotenv(monkeypatch, tmp_path):
    import uvicorn

    application = object()
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.setattr(local_server, "build_application", lambda args: application)
    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    local_server.main(["jobs", "--state-dir", str(tmp_path), "--port", "8123"])
    assert os.environ["PYTHON_DOTENV_DISABLED"] == "1"
    run.assert_called_once_with(
        application, host="127.0.0.1", port=8123, proxy_headers=False
    )
    run.reset_mock()
    with pytest.raises(SystemExit):
        local_server.main(["jobs", "--state-dir", str(tmp_path), "--port", "0"])
    run.assert_not_called()
