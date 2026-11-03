#!/usr/bin/env python3
"""Copy a completed warehouse into isolated datasets; publication is opt-in.

Run only with all warehouse writers stopped. Tables use one time-travel instant;
views are rebound to copied datasets. No source mutation or destination overwrite.
The archive health manifest is written last, after parity and coverage checks.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.seasons import ARCHIVE_SEASONS, dataset_for_season  # noqa: E402
from google.api_core.exceptions import NotFound  # noqa: E402
from google.cloud import bigquery  # noqa: E402
from sqlglot import exp, parse  # noqa: E402
from sqlglot.errors import SqlglotError  # noqa: E402
from sqlglot.optimizer.scope import traverse_scope  # noqa: E402

DATASETS = ("nba_bronze", "nba_silver", "nba_gold", "nba_agent", "nba_metadata")


def archive_mapping(project, season):
    if (
        not re.fullmatch(r"[a-z][a-z0-9-]{4,62}", project)
        or season not in ARCHIVE_SEASONS
    ):
        raise ValueError("Expected a valid project and supported archive season")
    return {
        f"{project}.{d}": f"{project}.{dataset_for_season(d, season)}" for d in DATASETS
    }


def _view_sql_and_dependencies(sql, mapping):
    """Parse physical table sources; leave CTEs, literals and aliases intact."""
    try:
        statements = parse(sql, read="bigquery")
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise ValueError("Archive views must contain one SELECT query")
        query = statements[0]
        project = next(iter(mapping)).split(".")[0]
        dependencies = set()
        for scope in traverse_scope(query):
            column_bindings = {}
            for source in scope.sources.values():
                if not isinstance(source, exp.Table):
                    continue  # CTEs and subqueries have their own traversed scope.
                if not isinstance(source.this, exp.Identifier) or not source.db:
                    raise ValueError("View has an external or unsupported dependency")
                dataset = f"{source.catalog or project}.{source.db}"
                target = mapping.get(dataset)
                if target is None:
                    raise ValueError(
                        f"View has an external or unsupported dependency: {dataset}"
                    )
                target_project, target_dataset = target.split(".")
                dependencies.add(f"{target}.{source.name}")
                if not source.alias:
                    column_bindings[f"{dataset}.{source.name}"] = (
                        target_project,
                        target_dataset,
                        source.name,
                    )
                source.set("catalog", exp.to_identifier(target_project, quoted=True))
                source.set("db", exp.to_identifier(target_dataset, quoted=True))
                source.set("this", exp.to_identifier(source.name, quoted=True))
                source.meta["quoted_table"] = True
            for column in scope.columns:
                if column.db:
                    binding = column_bindings.get(
                        f"{column.catalog or project}.{column.db}.{column.table}"
                    )
                    if binding:
                        column.set(
                            "catalog", exp.to_identifier(binding[0], quoted=True)
                        )
                        column.set("db", exp.to_identifier(binding[1], quoted=True))
                elif "." in column.table:
                    key = column.table
                    if key.count(".") == 1:
                        key = project + "." + key
                    binding = column_bindings.get(key)
                    if binding:
                        column.set(
                            "table", exp.to_identifier(".".join(binding), quoted=True)
                        )
        return query.sql(dialect="bigquery"), dependencies
    except SqlglotError as exc:
        raise ValueError("Unsupported archive view SQL") from exc


def rebind_view(sql, mapping):
    return _view_sql_and_dependencies(sql, mapping)[0]


def ordered_views(inventory, mapping):
    """Bind dependencies inside the archive and create parent views first."""
    known = {f"{target}.{table.table_id}" for table, _, _, target in inventory}
    pending = {}
    for table, _, _, target in inventory:
        if table.table_type == "VIEW":
            sql, references = _view_sql_and_dependencies(table.view_query, mapping)
            if any(ref.count(".") != 2 or ref not in known for ref in references):
                raise ValueError(
                    f"View has an external or unsupported dependency: {table.table_id}"
                )
            pending[f"{target}.{table.table_id}"] = (sql, references)
    ordered = []
    while pending:
        ready = [
            name
            for name, (_, refs) in pending.items()
            if not refs.intersection(pending)
        ]
        if not ready:
            raise ValueError("Cyclic archive view dependencies")
        for name in ready:
            sql, _ = pending.pop(name)
            ordered.append((name, sql))
    return ordered


def rows(client, sql):
    return [
        dict(r)
        for r in client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                maximum_bytes_billed=10_000_000_000, use_query_cache=False
            ),
        ).result(timeout=300)
    ]


def fingerprint(client, table):
    if "@" in table:
        table, millis = table.rsplit("@", 1)
        relation = (
            f"`{table}` AS t FOR SYSTEM_TIME AS OF TIMESTAMP_MILLIS({int(millis)})"
        )
    else:
        relation = f"`{table}` AS t"
    return rows(
        client,
        f"SELECT COUNT(*) AS n, BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(t))) AS h FROM {relation}",
    )[0]


def copy_archive(
    client,
    project,
    season,
    *,
    publish=False,
    writers_stopped=False,
    resume_snapshot_ms=None,
):
    if resume_snapshot_ms is not None:
        if not publish or not writers_stopped or type(resume_snapshot_ms) is not int:
            raise ValueError(
                "Explicit recovery requires publication, stopped writers and an integer snapshot"
            )
        age = int(datetime.now(UTC).timestamp() * 1000) - resume_snapshot_ms
        if not 0 < age < 7 * 24 * 60 * 60 * 1000:
            raise ValueError("Recovery snapshot must be in the previous seven days")
    mapping = archive_mapping(project, season)
    inventory = []
    existing = {}
    for source, target in mapping.items():
        dataset = client.get_dataset(source)
        try:
            client.get_dataset(target)
        except NotFound:
            pass
        else:
            if resume_snapshot_ms is None:
                raise ValueError(
                    f"Destination already exists: {target}; inspect partial runs manually"
                )
            existing[target] = {
                t.table_id: t.table_type for t in client.list_tables(target)
            }
            if "historical_backfill_manifest" in existing[target]:
                raise ValueError(
                    "An archive manifest already exists; recovery cannot modify a published archive"
                )
        for item in client.list_tables(source):
            table = client.get_table(item.reference)
            if table.table_type not in {"TABLE", "VIEW"}:
                raise ValueError(f"Unsupported object type: {table.full_table_id}")
            if table.table_id == "historical_backfill_manifest":
                raise ValueError("Source already contains an archive manifest")
            inventory.append((table, dataset.location, source, target))
    for target, objects in existing.items():
        expected = {
            table.table_id: table.table_type
            for table, _, _, dest in inventory
            if dest == target
        }
        if any(expected.get(name) != kind for name, kind in objects.items()):
            raise ValueError("Unexpected object in partial archive; inspect manually")
    views = ordered_views(inventory, mapping)
    if not inventory:
        raise ValueError("Empty warehouse")
    plan = {
        "season": season,
        "method": "warehouse_copy",
        "objects": len(inventory),
        "datasets": mapping,
    }
    if not publish:
        return plan
    if not writers_stopped:
        raise ValueError(
            "Stop all writers and pass --writers-stopped before publication"
        )
    # A single instant preserves cross-table consistency; time travel must be available.
    snapshot_ms = resume_snapshot_ms or int(datetime.now(UTC).timestamp() * 1000)
    for source, target in mapping.items():
        if target in existing:
            continue
        ds = bigquery.Dataset(target)
        ds.location = client.get_dataset(source).location
        client.create_dataset(ds)
    checks = {}
    for table, _, source, target in inventory:
        if table.table_type != "TABLE":
            continue
        src = f"{source}.{table.table_id}@{snapshot_ms}"
        dest = f"{target}.{table.table_id}"
        before = fingerprint(client, src)
        if resume_snapshot_ms is not None and before != fingerprint(
            client, f"{source}.{table.table_id}"
        ):
            raise ValueError(
                "Source changed since recovery snapshot; no manifest published"
            )
        if table.table_id not in existing.get(target, {}):
            client.copy_table(
                src,
                dest,
                job_config=bigquery.CopyJobConfig(write_disposition="WRITE_EMPTY"),
            ).result(timeout=300)
        after = fingerprint(client, dest)
        if before != after:
            raise ValueError(f"Copy parity failed: {dest}")
        checks[dest] = after
    for name, sql in views:
        target, table_id = name.rsplit(".", 1)
        if table_id in existing.get(target, {}):
            if client.get_table(name).view_query != sql:
                raise ValueError(
                    "Existing archive view differs; no overwrite permitted"
                )
            continue
        view = bigquery.Table(name)
        view.view_query = sql
        view.view_use_legacy_sql = False
        client.create_table(view)
    for table, _, source, target in inventory:
        if table.table_type == "VIEW":
            before = fingerprint(client, f"{source}.{table.table_id}")
            after = fingerprint(client, f"{target}.{table.table_id}")
            if before != after:
                raise ValueError(f"View parity failed: {table.table_id}")
            checks[f"{target}.{table.table_id}"] = after
    gold = mapping[f"{project}.nba_gold"]
    games = rows(
        client, f"SELECT game_id, season, winning_team_abbr FROM `{gold}.dim_game`"
    )
    validate_games(games, season)
    stats = f"{gold}.fct_player_game_stats"
    if not checks.get(stats, {}).get("n"):
        raise ValueError("Missing player statistics")
    injury_table = f"{mapping[f'{project}.nba_bronze']}.raw_player_injury_reports"
    injury_count = checks.get(injury_table, {}).get("n", 0)
    report = {
        **plan,
        "snapshot_ms": snapshot_ms,
        "checks": checks,
        "row_counts": {"injury_reports": injury_count},
        "validated_assets": ["stats"],
        "limitations": [
            "Copied warehouse publication; does not reconstruct historical knowledge times.",
            "Similarity health is unavailable until separately validated.",
        ],
    }
    if injury_count:
        silver = mapping[f"{project}.nba_silver"]
        coverage = rows(
            client,
            f"SELECT MAX(DATE(report_timestamp_utc)) AS latest_report_date FROM `{silver}.stg_player_injury_reports_clean` WHERE season='{season}'",
        )[0]
        if coverage["latest_report_date"]:
            report["injury_coverage"] = {k: str(v) for k, v in coverage.items()}
            report["validated_assets"].append("injuries")
    # Detect writers or DDL changes before recording publication success.
    for table, _, source, _ in inventory:
        if client.get_table(f"{source}.{table.table_id}").etag != table.etag:
            raise ValueError("Source changed during archive; no manifest published")
    as_of = rows(client, f"SELECT MAX(game_date) AS d FROM `{gold}.dim_game`")[0]["d"]
    manifest = f"{mapping[f'{project}.nba_metadata']}.historical_backfill_manifest"
    client.load_table_from_json(
        [
            {
                "season": season,
                "published_at_utc": datetime.now(UTC).isoformat(),
                "as_of_date": str(as_of),
                "report_json": json.dumps(report, default=str),
            }
        ],
        manifest,
        job_config=bigquery.LoadJobConfig(
            write_disposition="WRITE_EMPTY",
            schema=[
                bigquery.SchemaField("season", "STRING"),
                bigquery.SchemaField("published_at_utc", "TIMESTAMP"),
                bigquery.SchemaField("as_of_date", "DATE"),
                bigquery.SchemaField("report_json", "STRING"),
            ],
        ),
    ).result(timeout=90)
    return report


def validate_games(games, season):
    ids = [g["game_id"] for g in games]
    if len(ids) != len(set(ids)) or any(
        g["season"] != season or not g["winning_team_abbr"] for g in games
    ):
        raise ValueError("Invalid season, duplicate or unfinished games")
    regular = [g for g in ids if g.startswith("002" + season[2:4])]
    playoffs = [g for g in ids if g.startswith("004" + season[2:4])]
    series = {}
    for g in games:
        if g["game_id"] in playoffs:
            key = (g["game_id"][:-1], g["winning_team_abbr"])
            series[key] = series.get(key, 0) + 1
    series_ids = {k[0] for k in series}
    if (
        len(regular) != 1230
        or len(series_ids) != 15
        or any(
            max(v for (s, _), v in series.items() if s == sid) != 4
            for sid in series_ids
        )
    ):
        raise ValueError("Expected 1230 regular games and 15 completed playoff series")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--season", choices=ARCHIVE_SEASONS, default="2025-26")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--writers-stopped", action="store_true")
    parser.add_argument(
        "--resume-snapshot-ms",
        type=int,
        help="Explicit operator recovery only: original audited copy-job snapshot; never overwrites existing objects",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            copy_archive(
                bigquery.Client(project=args.project),
                args.project,
                args.season,
                publish=args.publish,
                writers_stopped=args.writers_stopped,
                resume_snapshot_ms=args.resume_snapshot_ms,
            ),
            default=str,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
