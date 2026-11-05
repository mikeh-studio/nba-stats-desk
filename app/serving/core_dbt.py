"""Generate a closed dbt project for the core profile, never the legacy project."""

import json
import re
from pathlib import Path

from app.serving.core_sql import PROFILE, compile_candidate

STAGING_ASSETS = dict(
    games="accepted_game_results",
    appearances="accepted_player_appearances",
    participants="participant_manifest",
    players="dim_player",
    teams="dim_team",
)


def staging_schema(name, *, provider="espn"):
    if provider not in {"espn", "nba_api"}:
        raise ValueError("unsupported source provider")
    integers = set(
        "player_id team_id home_team_id away_team_id home_score away_score min pts fgm fga fg3m fg3a ftm fta oreb dreb reb ast stl blk tov pf plus_minus".split()
    )
    return {
        key: "BOOL"
        if key == "played"
        else "FLOAT64"
        if key == "min" and provider == "nba_api"
        else "INT64"
        if key in integers
        else "DATE"
        if key == "game_date"
        else "TIMESTAMP"
        if key in {"source_captured_at", "source_verified_at"}
        else "STRING"
        for key in PROFILE["assets"][STAGING_ASSETS[name]]["required_fields"]
    }


def isolated_dataset(value):
    if not re.fullmatch(r"nba_phase4_[a-z0-9_]{8,100}", value):
        raise ValueError("explicit isolated Phase 4 dataset required")
    return value


def target_dataset(value, namespace=None):
    if namespace is None:
        return isolated_dataset(value)
    if not re.fullmatch(r"nba_core_[a-z0-9_]{3,64}", namespace) or not re.fullmatch(
        re.escape(namespace) + r"_(?:input|build|release)_[0-9a-f]{16}", value
    ):
        raise ValueError("dataset outside explicit production target")
    return value


def generate(
    root,
    *,
    project,
    staging,
    candidate,
    materialized_at,
    namespace=None,
    provider="espn",
):
    root = Path(root)
    if root.exists():
        raise ValueError("dbt project already exists")
    for name in [staging, candidate]:
        target_dataset(name, namespace)
    sql = compile_candidate(
        project=project,
        staging_dataset=staging,
        candidate_dataset=candidate,
        materialized_at=materialized_at,
        provider=provider,
    )
    (root / "models").mkdir(parents=True)
    config = dict(
        name="core_rehearsal",
        version="1.0",
        profile="core_rehearsal",
        **{
            "config-version": 2,
            "model-paths": ["models"],
            "models": {
                "core_rehearsal": {"+materialized": "table", "+hours_to_expiration": 48}
            },
        },
    )
    (root / "dbt_project.yml").write_text(json.dumps(config, indent=2))
    profile = {
        "core_rehearsal": {
            "target": "isolated",
            "outputs": {
                "isolated": dict(
                    type="bigquery",
                    method="oauth",
                    project=project,
                    dataset=candidate,
                    location="US",
                    threads=2,
                    maximum_bytes_billed=268435456,
                    job_execution_timeout_seconds=120,
                    job_retries=0,
                )
            },
        }
    }
    (root / "profiles.yml").write_text(json.dumps(profile, indent=2))
    for name, query in sql.items():
        dependencies = "\n".join(
            "-- depends_on: {{ ref('" + d + "') }}"
            for d in PROFILE["assets"][name]["dependencies"]
        )
        (root / "models" / (name + ".sql")).write_text(
            dependencies + "\n" + query + "\n"
        )
    return sql


def audit_manifest(
    manifest,
    *,
    project,
    candidate,
    staging=None,
    materialized_at=None,
    namespace=None,
    provider="espn",
):
    """Reject unexpected resources, custom schemas, hooks and target aliases."""
    target_dataset(candidate, namespace)
    nodes = manifest["nodes"]
    if len(nodes) != len(PROFILE["assets"]) or manifest.get("sources"):
        raise ValueError("unexpected dbt resource set")
    seen = set()
    expected = (
        compile_candidate(
            project=project,
            staging_dataset=staging,
            candidate_dataset=candidate,
            materialized_at=materialized_at,
            provider=provider,
        )
        if staging is not None
        else None
    )
    for node in nodes.values():
        name = node["name"]
        config = node["config"]
        if (
            name not in PROFILE["assets"]
            or node["resource_type"] != "model"
            or node["database"] != project
            or node["schema"] != candidate
            or node["alias"] != name
            or config["materialized"] != "table"
            or config.get("pre-hook")
            or config.get("post-hook")
        ):
            raise ValueError("dbt target escapes candidate")
        seen.add(name)
        if expected is not None:
            actual = "\n".join(
                line
                for line in node["raw_code"].splitlines()
                if not line.startswith("-- depends_on:")
            )
            if actual.strip() != expected[name].strip():
                raise ValueError("dbt SQL differs from closed candidate compiler")
    if seen != set(PROFILE["assets"]):
        raise ValueError("dbt dependency closure differs")
