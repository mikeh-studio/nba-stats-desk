"""dbt execution and validated injury publication within the existing task boundary."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

from nba_pipeline_triage import summarize_subprocess_failure

logger = logging.getLogger("nba_pipeline")


def run_dbt_command(command: list[str], *, repo_root: Path, env: dict) -> None:
    """Run one dbt command, retaining bounded diagnostic output on failure."""
    from airflow.exceptions import AirflowException

    completed = subprocess.run(
        command,
        cwd=repo_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AirflowException(
            summarize_subprocess_failure(
                command=command,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        )


def run_dbt_build(
    merge_result: dict,
    *,
    get_config,
    get_project_id,
    get_dataset,
    get_dbt_repo_root,
    season: str,
    new_candidate_id,
) -> dict:
    """Run dbt models and tests after the bronze merges."""
    if not merge_result["should_build"]:
        logger.info("Skipping dbt build because no source domain produced rows")
        merge_result["dbt_status"] = "skipped"
        merge_result["dbt_build_scope"] = "skipped"
        return merge_result

    repo_root = get_dbt_repo_root()
    profiles_dir = repo_root / "dbt" / "profiles"
    target = get_config("DBT_TARGET", "dev")
    injury_only_build = merge_result.get(
        "injury_report_rows_loaded", 0
    ) > 0 and not merge_result.get("core_warehouse_changed", False)
    command = [
        "dbt",
        "build",
        "--project-dir",
        str(repo_root),
        "--profiles-dir",
        str(profiles_dir),
        "--target",
        target,
        "--exclude",
    ]
    command.append("stg_player_injury_reports_clean+")
    merge_result["dbt_build_scope"] = "injury_only" if injury_only_build else "core"

    env = os.environ.copy()
    env.setdefault("BQ_PROJECT", get_project_id())
    env.setdefault("BQ_DATASET_BRONZE", get_dataset("BQ_DATASET_BRONZE", "nba_bronze"))
    env.setdefault("BQ_DATASET_SILVER", get_dataset("BQ_DATASET_SILVER", "nba_silver"))
    env.setdefault("BQ_DATASET_GOLD", get_dataset("BQ_DATASET_GOLD", "nba_gold"))
    env.setdefault("BQ_DATASET_AGENT", get_dataset("BQ_DATASET_AGENT", "nba_agent"))
    env.setdefault("NBA_SEASON", season)
    merge_result["dbt_command"] = " ".join(command)
    if not injury_only_build:
        run_dbt_command(command, repo_root=repo_root, env=env)
        merge_result["dbt_status"] = "success"
    else:
        merge_result["dbt_status"] = "skipped"

    return publish_injury_models(
        merge_result,
        command=command,
        repo_root=repo_root,
        env=env,
        get_project_id=get_project_id,
        get_dataset=get_dataset,
        new_candidate_id=new_candidate_id,
    )


def publish_injury_models(
    merge_result: dict,
    *,
    command: list[str],
    repo_root: Path,
    env: dict,
    get_project_id,
    get_dataset,
    new_candidate_id,
) -> dict:
    # dbt writes the injury branch and dependent agent context to unique
    # candidates. Tests finish before any of these become serving tables.
    from google.cloud import bigquery as bq

    from publication import expire_candidate, publish_candidates

    suffix = "_candidate_" + new_candidate_id().hex
    candidate_command = command[:-1] + [
        "--select",
        "stg_player_injury_reports_clean+",
        "--vars",
        json.dumps({"injury_publication_suffix": suffix}),
    ]
    candidates = [
        (
            f"{get_project_id()}.{get_dataset(dataset_key, default)}.{name}",
            f"{get_project_id()}.{get_dataset(dataset_key, default)}.{name}{suffix}",
        )
        for dataset_key, default, name in (
            ("BQ_DATASET_SILVER", "nba_silver", "stg_player_injury_reports_clean"),
            ("BQ_DATASET_GOLD", "nba_gold", "player_availability_current"),
            ("BQ_DATASET_AGENT", "nba_agent", "agent_player_search"),
            ("BQ_DATASET_GOLD", "nba_gold", "what_changed_injury_reports"),
        )
    ]
    client = None
    try:
        client = bq.Client(project=get_project_id())
        run_dbt_command(candidate_command, repo_root=repo_root, env=env)
        for _, candidate_id in candidates:
            expire_candidate(client, candidate_id)
        publish_candidates(client, candidates)
        merge_result["injury_dbt_status"] = "success"
    except Exception:
        logger.exception("Injury model publish failed; keeping previous serving tables")
        merge_result["injury_status"] = "failed_non_blocking"
        merge_result["injury_dbt_status"] = "failed_non_blocking"
    finally:
        # Failed dbt builds can leave some candidate tables behind.
        from google.api_core.exceptions import NotFound

        for _, candidate_id in candidates if client is not None else []:
            try:
                expire_candidate(client, candidate_id)
            except NotFound:
                pass
            except Exception:
                logger.warning(
                    "Could not expire injury candidate %s",
                    candidate_id,
                    exc_info=True,
                )
    return merge_result
