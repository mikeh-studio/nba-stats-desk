#!/usr/bin/env python3
"""Rebuild a verified completed-season repair in isolated, expiring datasets.

Creates candidates only. An immutable manifest records source versions, dbt
results and backups. Live promotion is a separate explicit operation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dags"))
from app.agent.semantic_source import snapshot_digest, snapshot_evidence  # noqa: E402
from scripts.repair_context_snapshot import repair  # noqa: E402

LAYERS = ("bronze", "silver", "gold", "agent")


def write_new(path, value):
    with path.open("x") as handle:
        json.dump(value, handle, indent=2, default=str)
        handle.write("\n")


def isolated_environment(project, suffix, run_dir):
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,62}", project) or not re.fullmatch(
        r"repair_[a-f0-9]{12}", suffix
    ):
        raise ValueError("Invalid isolated target")
    env = dict(
        os.environ,
        BQ_PROJECT=project,
        GCP_PROJECT_ID=project,
        DBT_TARGET_PATH=str(run_dir / "target"),
        DBT_LOG_PATH=str(run_dir / "logs"),
        DBT_PARTIAL_PARSE="false",
        DBT_TARGET="dev",
    )
    for layer in LAYERS:
        env["BQ_DATASET_" + layer.upper()] = f"nba_{layer}_{suffix}"
    env["BQ_DATASET"] = env["BQ_DATASET_GOLD"]
    env["BQ_METADATA_DATASET"] = f"nba_metadata_{suffix}"
    return env


def validate_manifest(manifest, datasets):
    for node in [*manifest["nodes"].values(), *manifest["sources"].values()]:
        if node.get("resource_type") not in ("model", "source"):
            continue
        if node.get("schema") not in datasets:
            raise ValueError("Compiled relation escaped isolated datasets")


def raw_repaired_rows(snapshot, schema):
    """Map verified official fields without overwriting missing input with zero."""
    fields = {f.name for f in schema}
    output = []
    for row in snapshot["rows"]:
        item = {k: v for k, v in row.items() if k in fields}
        for name, made, attempted in [
            ("fg_pct", "fgm", "fga"),
            ("fg3_pct", "fg3m", "fg3a"),
            ("ft_pct", "ftm", "fta"),
        ]:
            if name in fields:
                item[name] = row[made] / row[attempted] if row[attempted] else 0.0
        item["ingested_at_utc"] = snapshot["source_timestamp"]
        if fields - set(item):
            raise ValueError(
                "Repair does not supply all raw game-log columns: "
                + str(sorted(fields - set(item)))
            )
        output.append(item)
    return output


def stage(args):
    from google.cloud import bigquery

    snapshot, audit = repair(args.source_dir, args.original)
    snapshot_evidence(snapshot)
    run = args.run_dir.resolve()
    run.mkdir(parents=True, exist_ok=False)
    write_new(run / "snapshot.json", snapshot)
    write_new(run / "repair-audit.json", audit)
    suffix = "repair_" + uuid4().hex[:12]
    env = isolated_environment(args.project, suffix, run)
    client = bigquery.Client(project=args.project)
    plan = dict(
        version=1,
        project=args.project,
        suffix=suffix,
        season="2025-26",
        created_at=datetime.now(timezone.utc).isoformat(),
        status="preparing",
        snapshot_sha256=snapshot["sha256"],
        sources=[],
        candidates=[],
        views=[],
    )
    write_new(run / "started.json", plan)
    for layer in LAYERS:
        dataset = bigquery.Dataset(
            f"{args.project}.{env['BQ_DATASET_' + layer.upper()]}"
        )
        dataset.location = args.location
        dataset.default_table_expiration_ms = 7 * 24 * 60 * 60 * 1000
        client.create_dataset(dataset)
    # Snapshot source tables into isolated bronze; no source writes.
    for entry in client.list_tables(f"{args.project}.nba_bronze"):
        if not entry.table_id.startswith("raw_"):
            continue
        source = f"{args.project}.nba_bronze.{entry.table_id}"
        target = f"{args.project}.{env['BQ_DATASET_BRONZE']}.{entry.table_id}"
        table = client.get_table(source)
        client.copy_table(
            source,
            target,
            job_config=bigquery.CopyJobConfig(write_disposition="WRITE_EMPTY"),
        ).result()
        if client.get_table(source).etag != table.etag:
            raise ValueError("Source changed during snapshot; start a new run")
        plan["sources"].append(dict(active=source, etag=table.etag))
    raw = f"{args.project}.{env['BQ_DATASET_BRONZE']}.raw_game_logs"
    schema = client.get_table(raw).schema
    # The completed season must be the only season in this current raw table.
    scopes = list(
        client.query(
            f"SELECT DISTINCT season FROM `{raw}`",
            job_config=bigquery.QueryJobConfig(maximum_bytes_billed=100_000_000),
        ).result()
    )
    if {r["season"] for r in scopes} != {"2025-26"}:
        raise ValueError(
            "Current raw table contains other seasons; refusing replacement"
        )
    client.load_table_from_json(
        raw_repaired_rows(snapshot, schema),
        raw,
        job_config=bigquery.LoadJobConfig(
            schema=schema, write_disposition="WRITE_TRUNCATE"
        ),
    ).result()
    command = [
        str(args.dbt),
        "parse",
        "--project-dir",
        str(ROOT),
        "--profiles-dir",
        str(ROOT / "dbt/profiles"),
        "--target",
        "dev",
    ]
    subprocess.run(command, env=env, check=True)
    manifest = json.loads((run / "target/manifest.json").read_text())
    validate_manifest(
        manifest, {env["BQ_DATASET_" + layer.upper()] for layer in LAYERS}
    )
    # Back up each existing table before candidate computation; later publication
    # must prove the live version still equals this backup inside its transaction.
    relations = []
    for node in manifest["nodes"].values():
        if node.get("resource_type") != "model":
            continue
        layer = next(
            name
            for name in LAYERS
            if node["schema"] == env["BQ_DATASET_" + name.upper()]
        )
        active = f"{args.project}.nba_{layer}.{node['alias']}"
        candidate = f"{args.project}.{node['schema']}.{node['alias']}"
        if node["config"]["materialized"] == "view":
            plan["views"].append(dict(active=active, candidate=candidate))
            continue
        relations.append((active, candidate))
    relations.append((f"{args.project}.nba_bronze.raw_game_logs", raw))
    for name in ("player_similarity_features", "player_archetypes"):
        relations.append(
            (
                f"{args.project}.nba_gold.{name}",
                f"{args.project}.{env['BQ_DATASET_GOLD']}.{name}",
            )
        )
    from google.api_core.exceptions import NotFound

    for active, candidate in relations:
        try:
            table = client.get_table(active)
        except NotFound:
            continue  # New models are not automatically published.
        if table.table_type != "TABLE":
            raise ValueError("Expected an active table")
        backup = candidate + "_before"
        client.copy_table(
            active,
            backup,
            job_config=bigquery.CopyJobConfig(write_disposition="WRITE_EMPTY"),
        ).result()
        if client.get_table(active).etag != table.etag:
            raise ValueError("Live table changed during backup")
        plan["candidates"].append(
            dict(active=active, candidate=candidate, backup=backup, etag=table.etag)
        )
    write_new(run / "baseline.json", plan)
    finish_stage(args, plan, env, run, snapshot, client)


def finish_stage(args, plan, env, run, snapshot, client):
    from google.cloud import bigquery
    from scripts.repair_game_schedule import reconcile_schedule

    snapshot, schedule, schedule_audit = reconcile_schedule(
        snapshot, json.loads(args.schedule.read_text())
    )
    write_new(run / "schedule-reconciliation.json", schedule_audit)
    write_new(run / "scheduled-snapshot.json", snapshot)
    plan["snapshot_sha256"] = snapshot["sha256"]
    raw = f"{args.project}.{env['BQ_DATASET_BRONZE']}.raw_game_logs"
    schema = client.get_table(raw).schema
    client.load_table_from_json(
        raw_repaired_rows(snapshot, schema),
        raw,
        job_config=bigquery.LoadJobConfig(
            schema=schema, write_disposition="WRITE_TRUNCATE"
        ),
    ).result()
    active = f"{args.project}.nba_bronze.raw_schedule"
    candidate = f"{args.project}.{env['BQ_DATASET_BRONZE']}.raw_schedule"
    if not any(pair["active"] == active for pair in plan["candidates"]):
        live = client.get_table(active)
        client.copy_table(
            active,
            candidate + "_before",
            job_config=bigquery.CopyJobConfig(write_disposition="WRITE_EMPTY"),
        ).result()
        plan["candidates"].append(
            dict(
                active=active,
                candidate=candidate,
                backup=candidate + "_before",
                etag=live.etag,
            )
        )
    write_new(run / "prepared-baseline.json", plan)
    # Keep unrelated/preseason/upcoming rows, replacing verified date/team/opponent keys.
    keys = {
        (row["schedule_date"], row["team_abbr"], row["opponent_abbr"])
        for row in schedule
    }
    prior = [
        dict(row)
        for row in client.query(
            f"SELECT * FROM `{candidate}`",
            job_config=bigquery.QueryJobConfig(maximum_bytes_billed=100_000_000),
        ).result()
    ]
    prior = [
        {
            key: (value.isoformat() if hasattr(value, "isoformat") else value)
            for key, value in row.items()
        }
        for row in prior
        if (str(row["schedule_date"]), row["team_abbr"], row["opponent_abbr"])
        not in keys
    ]
    client.load_table_from_json(
        prior + schedule,
        candidate,
        job_config=bigquery.LoadJobConfig(
            schema=client.get_table(candidate).schema,
            write_disposition="WRITE_TRUNCATE",
        ),
    ).result()

    command = [
        str(args.dbt),
        "build",
        "--project-dir",
        str(ROOT),
        "--profiles-dir",
        str(ROOT / "dbt/profiles"),
        "--target",
        "dev",
    ]
    with (run / "dbt-build.log").open("x") as handle:
        subprocess.run(
            command, env=env, stdout=handle, stderr=subprocess.STDOUT, check=True
        )
    finalize_stage(args, plan, env, run, snapshot, client)


def finalize_stage(args, plan, env, run, snapshot, client):
    """Finish a successful dbt build; revalidate repaired facts before similarity publication."""
    from google.cloud import bigquery

    results = json.loads((run / "target/run_results.json").read_text())
    if not results["results"] or any(
        r["status"] not in ("success", "pass", "warn") for r in results["results"]
    ):
        raise ValueError("Every dbt model and test must pass before a repair is ready")
    # The rebuilt fact must match the verified repair at player-game grain.
    fact = f"{args.project}.{env['BQ_DATASET_GOLD']}.fct_player_game_stats"
    columns = [
        "season",
        "season_type",
        "game_id",
        "player_id",
        "game_date",
        "team_abbr",
        "opponent_abbr",
        "home_away",
        "pts",
        "reb",
        "ast",
        "stl",
        "blk",
        "tov",
        "min",
        "fgm",
        "fga",
        "fg3m",
        "fg3a",
        "ftm",
        "fta",
        "plus_minus",
    ]
    actual = [
        dict(r)
        for r in client.query(
            f"SELECT {', '.join(columns)} FROM `{fact}`",
            job_config=bigquery.QueryJobConfig(maximum_bytes_billed=100_000_000),
        ).result()
    ]

    def normalize(rows):
        return sorted(
            [
                tuple(str(r[k]) if k == "game_date" else r[k] for k in columns)
                for r in rows
            ]
        )

    if normalize(actual) != normalize(snapshot["rows"]):
        raise ValueError("Rebuilt fact differs from validated repair")
    import nba_pipeline

    features = client.query(
        f"SELECT * FROM `{args.project}.{env['BQ_DATASET_GOLD']}.player_similarity_feature_input`",
        job_config=bigquery.QueryJobConfig(maximum_bytes_billed=100_000_000),
    ).to_dataframe(create_bqstorage_client=False)
    outputs = nba_pipeline.build_player_similarity_outputs(features)
    nba_pipeline.write_player_similarity_tables(
        client,
        features_table_id=f"{args.project}.{env['BQ_DATASET_GOLD']}.player_similarity_features",
        archetypes_table_id=f"{args.project}.{env['BQ_DATASET_GOLD']}.player_archetypes",
        features_df=outputs["features"],
        archetypes_df=outputs["archetypes"],
    )
    for pair in plan["candidates"]:
        pair["candidate_etag"] = client.get_table(pair["candidate"]).etag
    plan.update(
        status="validated",
        dbt_results=len(results["results"]),
        warnings=[r for r in results["results"] if r["status"] == "warn"],
        player_rows=len(actual),
        similarity_players=len(outputs["features"]),
        note="Candidate tables only; live tables and existing views remain unchanged. Similarity features and archetypes have been rebuilt together.",
    )
    plan["sha256"] = snapshot_digest(plan)
    write_new(run / "validated.json", plan)
    print(
        json.dumps(
            {
                "status": plan["status"],
                "run_dir": str(run),
                "tables": len(plan["candidates"]),
                "rows": len(actual),
            }
        )
    )


def resume_stage(args):
    from app.agent.semantic_source import load_snapshot
    from google.cloud import bigquery

    base = args.resume_from.resolve()
    prepared = (base / "prepared-baseline.json").exists()
    plan = json.loads(
        (base / ("prepared-baseline.json" if prepared else "baseline.json")).read_text()
    )
    snapshot, _ = load_snapshot(
        base / ("scheduled-snapshot.json" if prepared else "snapshot.json")
    )
    if (
        plan["status"] != "preparing"
        or plan["project"] != args.project
        or plan["snapshot_sha256"] != snapshot["sha256"]
    ):
        raise ValueError("Invalid resume baseline")
    run = args.run_dir.resolve()
    run.mkdir(parents=True, exist_ok=False)
    env = isolated_environment(args.project, plan["suffix"], run)
    client = bigquery.Client(project=args.project)
    for record in [*plan["sources"], *plan["candidates"]]:
        if client.get_table(record["active"]).etag != record["etag"]:
            raise ValueError("Live source changed; create a fresh stage")
    # Older preparing manifests may predate the similarity stage.
    for name in ("player_similarity_features", "player_archetypes"):
        active = f"{args.project}.nba_gold.{name}"
        if any(p["active"] == active for p in plan["candidates"]):
            continue
        candidate = f"{args.project}.{env['BQ_DATASET_GOLD']}.{name}"
        table = client.get_table(active)
        client.copy_table(
            active,
            candidate + "_before",
            job_config=bigquery.CopyJobConfig(write_disposition="WRITE_EMPTY"),
        ).result()
        plan["candidates"].append(
            dict(
                active=active,
                candidate=candidate,
                backup=candidate + "_before",
                etag=table.etag,
            )
        )
    write_new(run / "snapshot.json", snapshot)
    write_new(run / "baseline.json", plan)
    subprocess.run(
        [
            str(args.dbt),
            "parse",
            "--project-dir",
            str(ROOT),
            "--profiles-dir",
            str(ROOT / "dbt/profiles"),
            "--target",
            "dev",
        ],
        env=env,
        check=True,
    )
    validate_manifest(
        json.loads((run / "target/manifest.json").read_text()),
        {env["BQ_DATASET_" + layer.upper()] for layer in LAYERS},
    )
    finish_stage(args, plan, env, run, snapshot, client)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--schedule", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--location", default="US")
    parser.add_argument("--dbt", type=Path, default=ROOT / ".venv-airflow/bin/dbt")
    parser.add_argument("--resume-from", type=Path)
    args = parser.parse_args()
    (resume_stage if args.resume_from else stage)(args)


if __name__ == "__main__":
    main()
