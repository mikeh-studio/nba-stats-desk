from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dags"))

import dbt_builds


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path / "airflow"))
    pytest.importorskip("airflow")
    return dict(
        get_config=lambda key, default=None: default,
        get_project_id=lambda: "demo",
        get_dataset=lambda key, default: default,
        get_dbt_repo_root=lambda: tmp_path,
        season="2025-26",
        new_candidate_id=lambda: SimpleNamespace(hex="fixed"),
    )


def test_noop_build_never_resolves_config_or_creates_candidates():
    forbidden = Mock(side_effect=AssertionError("no-op must not perform work"))
    result = {"should_build": False}
    assert (
        dbt_builds.run_dbt_build(
            result,
            get_config=forbidden,
            get_project_id=forbidden,
            get_dataset=forbidden,
            get_dbt_repo_root=forbidden,
            season="2025-26",
            new_candidate_id=forbidden,
        )
        is result
    )
    assert result == {
        "should_build": False,
        "dbt_status": "skipped",
        "dbt_build_scope": "skipped",
    }
    forbidden.assert_not_called()


@pytest.mark.parametrize("failure", [None, "candidate_build", "publish", "cleanup"])
def test_injury_only_build_preserves_publication_and_cleanup_boundaries(
    monkeypatch, runtime, failure
):
    from google.cloud import bigquery

    import publication

    monkeypatch.setattr(bigquery, "Client", lambda **kwargs: object())
    events = []

    def run(command, **kwargs):
        events.append(("build", command))
        return SimpleNamespace(
            returncode=int(failure == "candidate_build"),
            stdout="",
            stderr="candidate failed",
        )

    def publish(client, candidates):
        events.append(("publish", candidates))
        if failure == "publish":
            raise RuntimeError("transaction failed")

    def expire(client, candidate):
        events.append(("expire", candidate))
        if failure == "cleanup" and any(event[0] == "publish" for event in events):
            raise RuntimeError("cleanup unavailable")

    monkeypatch.setattr(dbt_builds.subprocess, "run", run)
    monkeypatch.setattr(publication, "publish_candidates", publish)
    monkeypatch.setattr(publication, "expire_candidate", expire)
    result = dbt_builds.run_dbt_build(
        {
            "should_build": True,
            "core_warehouse_changed": False,
            "injury_report_rows_loaded": 3,
        },
        **runtime,
    )
    assert result["dbt_status"] == "skipped"
    assert result["dbt_build_scope"] == "injury_only"
    builds = [event for event in events if event[0] == "build"]
    assert len(builds) == 1
    assert "--select" in builds[0][1]
    assert '"injury_publication_suffix": "_candidate_fixed"' in builds[0][1][-1]
    assert len([event for event in events if event[0] == "expire"]) == (
        4 if failure == "candidate_build" else 8
    )
    if failure == "candidate_build":
        assert not any(event[0] == "publish" for event in events)
    else:
        assert [event[0] for event in events[:6]] == [
            "build",
            "expire",
            "expire",
            "expire",
            "expire",
            "publish",
        ]
        candidates = events[5][1]
        assert [active.split(".")[-1] for active, _ in candidates] == [
            "stg_player_injury_reports_clean",
            "player_availability_current",
            "agent_player_search",
            "what_changed_injury_reports",
        ]
        assert all(
            candidate == active + "_candidate_fixed" for active, candidate in candidates
        )
    if failure in {"candidate_build", "publish"}:
        assert (
            result["injury_status"]
            == result["injury_dbt_status"]
            == "failed_non_blocking"
        )
    else:
        assert result["injury_dbt_status"] == "success"


def test_dbt_command_preserves_environment_and_raises_diagnostics(monkeypatch, runtime):
    from airflow.exceptions import AirflowException

    run = Mock(
        return_value=SimpleNamespace(
            returncode=2, stdout="build output", stderr="warehouse error"
        )
    )
    monkeypatch.setattr(dbt_builds.subprocess, "run", run)
    with pytest.raises(AirflowException, match="warehouse error"):
        dbt_builds.run_dbt_command(
            ["dbt", "build"],
            repo_root=runtime["get_dbt_repo_root"](),
            env={"EXPLICIT": "value"},
        )
    assert run.call_args.kwargs["env"] == {"EXPLICIT": "value"}
    assert run.call_args.kwargs["check"] is False
    assert run.call_args.kwargs["capture_output"] is True
