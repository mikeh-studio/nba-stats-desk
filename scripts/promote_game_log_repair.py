#!/usr/bin/env python3
"""Explicitly promote an unchanged validated repair with transactional baselines.

All live data changes commit together. Expiring before-tables remain available
for a separately reviewed rollback; failed assertions preserve live rows.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.agent.semantic_source import snapshot_digest  # noqa: E402
from dags.publication import quoted_table  # noqa: E402


def validate_plan(plan, expected_sha):
    if (
        plan.get("sha256") != expected_sha
        or snapshot_digest(plan) != expected_sha
        or plan.get("status") != "validated"
    ):
        raise ValueError("Expected an unchanged validated manifest")
    if not plan.get("similarity_players"):
        raise ValueError(
            "Dependent similarity outputs must be validated before promotion"
        )
    project, suffix = plan["project"], plan["suffix"]
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,62}", project) or not re.fullmatch(
        r"repair_[a-f0-9]{12}", suffix
    ):
        raise ValueError("Invalid repair target")
    if not plan.get("candidates"):
        raise ValueError("Empty candidate set")
    seen = set()
    for pair in plan["candidates"]:
        active = pair["active"]
        match = re.fullmatch(
            re.escape(project) + r"\.nba_(bronze|silver|gold|agent)\.([a-z][a-z0-9_]*)",
            active,
        )
        if not match or active in seen:
            raise ValueError("Invalid or duplicate active relation")
        layer, name = match.groups()
        expected = f"{project}.nba_{layer}_{suffix}.{name}"
        if pair["candidate"] != expected or pair["backup"] != expected + "_before":
            raise ValueError("Candidate or backup escaped isolated repair")
        if not pair.get("candidate_etag"):
            raise ValueError("Candidate version missing")
        seen.add(active)
    required = {
        "nba_bronze.raw_game_logs",
        "nba_bronze.raw_schedule",
        "nba_silver.stg_game_logs_clean",
        "nba_silver.int_player_game_enriched",
        "nba_gold.fct_player_game_stats",
        "nba_gold.fct_team_game_scores",
        "nba_gold.dim_game",
        "nba_gold.player_similarity_features",
        "nba_gold.player_archetypes",
    }
    if not {f"{project}.{name}" for name in required} <= seen:
        raise ValueError("Required dependent tables missing")


def equal_rows_assertion(active, backup, columns):
    if not columns or any(
        not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name) for name in columns
    ):
        raise ValueError("Invalid comparison columns")
    fields = ", ".join(f"`{name}`" for name in columns)

    def bag(table):
        return f"SELECT TO_JSON_STRING(STRUCT({fields})) AS row_json, COUNT(*) AS copies FROM {quoted_table(table)} GROUP BY row_json"

    live, old = bag(active), bag(backup)
    return (
        f"ASSERT NOT EXISTS (({live} EXCEPT DISTINCT {old}) UNION ALL "
        f'({old} EXCEPT DISTINCT {live})) AS "Live baseline changed; repair aborted";'
    )


def transaction_sql(pairs, sources=()):
    statements = ["BEGIN TRANSACTION;"]
    for pair in [*sources, *pairs]:
        statements.append(
            equal_rows_assertion(
                pair["active"], pair["backup"], pair["baseline_columns"]
            )
        )
    for pair in pairs:
        fields = ", ".join("`" + name + "`" for name in pair["candidate_columns"])
        if any(
            not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name)
            for name in pair["candidate_columns"]
        ):
            raise ValueError("Invalid publication columns")
        statements.extend(
            [
                f"DELETE FROM {quoted_table(pair['active'])} WHERE TRUE;",
                f"INSERT INTO {quoted_table(pair['active'])} ({fields}) SELECT {fields} FROM {quoted_table(pair['candidate'])};",
            ]
        )
    statements.append("COMMIT TRANSACTION;")
    return "\n".join(statements)


def promote(plan, expected_sha, client):
    from google.cloud import bigquery

    validate_plan(plan, expected_sha)
    for source in plan["sources"]:
        if client.get_table(source["active"]).etag != source["etag"]:
            raise ValueError("An upstream source changed; rebuild candidates")
    pairs = []
    source_guards = []
    for source in plan["sources"]:
        if any(pair["active"] == source["active"] for pair in plan["candidates"]):
            continue
        project, dataset, table = source["active"].split(".")
        if (
            project != plan["project"]
            or dataset != "nba_bronze"
            or not table.startswith("raw_")
        ):
            raise ValueError("Invalid upstream source")
        backup = f"{project}.{dataset}_{plan['suffix']}.{table}"
        source_guards.append(
            dict(
                active=source["active"],
                backup=backup,
                baseline_columns=[f.name for f in client.get_table(backup).schema],
            )
        )
    for pair in plan["candidates"]:
        active, candidate, backup = (
            client.get_table(pair[k]) for k in ("active", "candidate", "backup")
        )
        if active.etag != pair["etag"] or candidate.etag != pair["candidate_etag"]:
            raise ValueError("Live or candidate table version changed")
        fields = {f.name: f for f in active.schema}
        proposed = {f.name: f for f in candidate.schema}
        if set(fields) - set(proposed):
            raise ValueError("Repair cannot remove active columns")
        for name, field in proposed.items():
            if name in fields and (field.field_type, field.mode, field.fields) != (
                fields[name].field_type,
                fields[name].mode,
                fields[name].fields,
            ):
                raise ValueError("Repair cannot change existing column types")
            if name not in fields and field.mode != "NULLABLE":
                raise ValueError("Only nullable additions are allowed")
        pairs.append(
            dict(
                pair,
                baseline_columns=[f.name for f in backup.schema],
                candidate_columns=list(proposed),
            )
        )
    # Schema additions cannot be transactional, but do not change existing rows.
    # Recheck every pair first so an incompatible later table cannot partially add schemas.
    for pair in pairs:
        active, candidate = (client.get_table(pair[k]) for k in ("active", "candidate"))
        names = {f.name for f in active.schema}
        additions = [f for f in candidate.schema if f.name not in names]
        if additions:
            active.schema = [*active.schema, *additions]
            client.update_table(active, ["schema"])
    sql = transaction_sql(pairs, source_guards)
    job = client.query(
        sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=3_000_000_000)
    )
    job.result(timeout=300)
    return {
        "status": "promoted",
        "job_id": job.job_id,
        "tables": len(pairs),
        "manifest_sha256": expected_sha,
        "rollback_backups": [p["backup"] for p in pairs],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if args.receipt.exists():
        raise ValueError("Receipt already exists; inspect prior run")
    plan = json.loads(args.manifest.read_text())
    validate_plan(plan, args.expected_sha256)
    from google.cloud import bigquery

    result = promote(
        plan, args.expected_sha256, bigquery.Client(project=plan["project"])
    )
    with args.receipt.open("x") as handle:
        json.dump(result, handle, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k != "rollback_backups"}))


if __name__ == "__main__":
    main()
