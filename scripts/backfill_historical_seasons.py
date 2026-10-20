#!/usr/bin/env python3
"""Validate historical NBA seasons locally, then build isolated archive datasets.

Default is extract/validate only. --publish creates season-suffixed datasets,
archives immutable inputs in GCS, loads empty bronze tables, and runs dbt build.
Existing tables are accepted only when their source fingerprint matches.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "dags")]

import pandas as pd  # noqa: E402
import yaml  # noqa: E402
from app.seasons import ARCHIVE_SEASONS, dataset_for_season, season_bounds  # noqa: E402
from google.api_core.exceptions import NotFound  # noqa: E402
from google.cloud import bigquery, storage  # noqa: E402
from nba_api.stats.endpoints import leaguegamelog  # noqa: E402
from scripts.historical_sources import (  # noqa: E402
    extract_injuries,
    resolve_injury_names,
    resolve_neutral_schedule,
)

import nba_pipeline as pipeline  # noqa: E402
import nba_source_contracts as contracts  # noqa: E402

HISTORICAL_SEASONS = ARCHIVE_SEASONS
PHASES = ("Regular Season", "Playoffs")
TABLES = {
    "game_logs": ("raw_game_logs", pipeline.get_game_logs_schema),
    "game_line_scores": ("raw_game_line_scores", pipeline.get_game_line_scores_schema),
    "schedule": ("raw_schedule", pipeline.get_schedule_schema),
    "player_reference": ("raw_player_reference", pipeline.get_player_reference_schema),
    "player_shot_locations": (
        "raw_player_shot_locations",
        pipeline.get_player_shot_locations_schema,
    ),
    "injury_reports": ("raw_player_injury_reports", pipeline.get_injury_report_schema),
}


def extract(season: str, directory: Path) -> None:
    """League-wide extraction includes players who are no longer active."""
    for phase in PHASES:
        tag = season + "_" + phase.replace(" ", "_")
        for domain in ("players", "teams", "shots"):
            path = directory / f"{tag}_{domain}.parquet"
            if path.exists():
                continue
            for attempt in range(3):
                try:
                    if domain == "players":
                        frame = pipeline.get_league_player_game_log(
                            season=season, season_type=phase, retries=1, timeout=30
                        )
                    elif domain == "teams":
                        frame = leaguegamelog.LeagueGameLog(
                            player_or_team_abbreviation="T",
                            season=season,
                            season_type_all_star=phase,
                            timeout=30,
                        ).get_data_frames()[0]
                    else:
                        frame = pipeline.get_player_shot_locations(
                            season=season, season_type=phase, retries=1, timeout=30
                        )
                    if frame.empty:
                        raise ValueError(
                            f"Empty {domain} response for {season} {phase}"
                        )
                    frame.to_parquet(path, index=False)
                    print(f"{season} {phase}: {len(frame)} {domain} rows", flush=True)
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(3)
            time.sleep(1)


def validate_frame(domain: str, frame: pd.DataFrame, season: str) -> dict:
    """Reuse production column/key checks with explicit historical bounds.

    Team totals originate from independent league team logs; period scoring is
    unavailable and stored as NULL. Do not apply the box-score period-sum rule
    to a source that has no periods.
    """
    contract = contracts.load_contract(domain, season=season)
    if domain == "game_line_scores":
        periods = [c for c in frame if c.startswith(("PTS_QTR", "PTS_OT"))]
        if not frame[periods].isna().all().all():
            raise ValueError(
                "Historical team-total inputs must leave unavailable periods NULL"
            )
        contract["source"] = "nba_api_league_team_game_logs"
        contract["rules"] = [
            r for r in contract["rules"] if r["name"] != "total_matches_period_sum"
        ]
    with tempfile.TemporaryDirectory() as temp:
        (Path(temp) / f"{domain}.yml").write_text(yaml.safe_dump(contract))
        validation = contracts.validate_source_contract(
            domain, frame, contract_dir=Path(temp)
        )
    if validation.result["fatal_count"] or not validation.quarantine_frame.empty:
        raise ValueError(
            f"{season} {domain}: rejected source rows: {validation.result}"
        )
    return validation.result


def reconcile_games(
    players: pd.DataFrame, teams: pd.DataFrame, season: str, phase: str
) -> dict:
    """Block partial seasons, missing team/player games, and scoring differences."""
    if players.empty or teams.empty:
        raise ValueError(f"{season} {phase}: empty game data")
    if (
        players.duplicated(["GAME_ID", "PLAYER_ID"]).any()
        or teams.duplicated(["GAME_ID", "TEAM_ID"]).any()
    ):
        raise ValueError("Duplicate player/team game keys")
    if set(players["SEASON"]) != {season} or set(players["SEASON_TYPE"]) != {phase}:
        raise ValueError("Player input season or phase mismatch")
    if set(players["GAME_ID"]) != set(teams["GAME_ID"]):
        raise ValueError("Player/team game coverage mismatch")
    if not teams.groupby("GAME_ID").size().eq(2).all():
        raise ValueError("Expected two teams for every game")
    game_prefix = ("002" if phase == "Regular Season" else "004") + season[2:4]
    if not players["GAME_ID"].astype(str).str.startswith(game_prefix).all():
        raise ValueError("Game IDs do not belong to the requested season/phase")
    if phase == "Regular Season" and (
        teams["GAME_ID"].nunique() != 1230
        or teams["TEAM_ID"].nunique() != 30
        or not teams.groupby("TEAM_ID").size().eq(82).all()
    ):
        raise ValueError(
            "Regular season must have 1230 games and 82 appearances for each of 30 teams"
        )
    if phase == "Playoffs":
        # Every completed best-of-seven series has exactly four winning games.
        series = (
            teams.assign(series=teams.GAME_ID.str[:-1])
            .query("WL == 'W'")
            .groupby(["series", "TEAM_ID"])
            .size()
            .groupby(level="series")
            .max()
        )
        if len(series) != 15 or not series.eq(4).all():
            raise ValueError("Playoffs must contain 15 completed series")
    p = players.groupby(["GAME_ID", "MATCHUP"], as_index=False).agg(
        PTS=("PTS", "sum"), GAME_DATE=("GAME_DATE", "first")
    )
    joined = p.merge(
        teams[["GAME_ID", "MATCHUP", "PTS", "GAME_DATE"]],
        on=["GAME_ID", "MATCHUP"],
        how="outer",
        suffixes=("_player", "_team"),
        indicator=True,
        validate="one_to_one",
    )
    if (
        not joined["_merge"].eq("both").all()
        or not joined.PTS_player.eq(joined.PTS_team).all()
        or not pd.to_datetime(joined.GAME_DATE_player)
        .eq(pd.to_datetime(joined.GAME_DATE_team))
        .all()
    ):
        raise ValueError(
            "Player totals/dates differ from independent official team logs"
        )
    return {
        "rows": len(players),
        "games": players.GAME_ID.nunique(),
        "players": players.PLAYER_ID.nunique(),
        "first_game": str(players.GAME_DATE.min())[:10],
        "last_game": str(players.GAME_DATE.max())[:10],
        "team_score_reconciliation": "passed",
    }


def prepare(season: str, directory: Path) -> tuple[dict[str, pd.DataFrame], dict]:
    player_parts, shot_parts = [], []
    report = {
        "season": season,
        "phases": {},
        "limitations": [
            "Team final scores verified against independent official team logs; quarter/overtime scoring unavailable (NULL).",
            "Player reference is the last observed team/name for this season; unobserved biography/anthropometry remains NULL.",
        ],
    }
    for phase in PHASES:
        tag = season + "_" + phase.replace(" ", "_")
        players = pd.read_parquet(directory / f"{tag}_players.parquet")
        teams = pd.read_parquet(directory / f"{tag}_teams.parquet")
        report["phases"][phase] = reconcile_games(players, teams, season, phase)
        player_parts.append(players)
        shots = pd.read_parquet(directory / f"{tag}_shots.parquet")
        if (
            shots.empty
            or set(shots.SEASON) != {season}
            or set(shots.SEASON_TYPE) != {phase}
        ):
            raise ValueError("Shot input season/phase mismatch or empty input")
        if set(shots.PLAYER_ID) != set(players.PLAYER_ID):
            raise ValueError("Shot profile/player game-log coverage mismatch")
        shot_parts.append(shots)
    logs = pd.concat(player_parts, ignore_index=True)
    totals = pipeline.derive_game_line_scores_from_game_logs(logs, season=season)
    for column in totals:
        if column.startswith(("PTS_QTR", "PTS_OT")):
            totals[column] = pd.Series(pd.NA, index=totals.index, dtype="Int64")
    injury_path = directory / f"{season}_injuries.parquet"
    injuries = (
        pd.read_parquet(injury_path)
        if injury_path.exists()
        else pd.DataFrame(columns=[f.name for f in pipeline.get_injury_report_schema()])
    )
    if not injuries.empty:
        injuries, repaired = resolve_injury_names(injuries, logs)
        report["injury_name_matches_repaired"] = repaired
    checks_path = directory / f"{season}_injury_source_checks.json"
    if not checks_path.exists():
        checks_path = directory / "injury_source_checks.json"
    if checks_path.exists():
        start, end = season_bounds(season)
        report["injury_source_checks"] = [
            row
            for row in json.loads(checks_path.read_text())
            if start.isoformat() <= row["date"] <= end.isoformat()
        ]
    if not injuries.empty:
        checks = report.get("injury_source_checks", [])
        report["injury_coverage"] = {
            "latest_report_date": str(
                pd.to_datetime(injuries.REPORT_DATE).max().date()
            ),
            "unmatched_player_rows": int(injuries.PLAYER_ID.isna().sum()),
            "reports_available": sum(row.get("status") == 200 for row in checks),
            "report_dates_checked": len(checks),
            "missing_report_dates": [
                row["date"] for row in checks if row.get("status") != 200
            ],
        }
    if injuries.empty:
        report["limitations"].append(
            "No historical injury reports were loaded; empty injury history does not imply healthy status. See injury_source_checks when present."
        )
    frames = {
        "game_logs": logs,
        "game_line_scores": totals,
        "schedule": resolve_neutral_schedule(
            season,
            pipeline.derive_schedule_from_game_logs(logs, season=season),
            directory,
        ),
        "player_reference": pipeline.derive_player_reference_from_game_logs(
            logs, season=season
        ),
        "player_shot_locations": pd.concat(shot_parts, ignore_index=True),
        "injury_reports": injuries,
    }
    report["validation"] = {
        domain: validate_frame(domain, frame, season)
        for domain, frame in frames.items()
        if not frame.empty
    }
    report["row_counts"] = {domain: len(frame) for domain, frame in frames.items()}
    report["as_of_date"] = str(logs.GAME_DATE.max())[:10]
    return frames, report


def warehouse_env(season: str, project: str, location: str) -> dict[str, str]:
    if season not in HISTORICAL_SEASONS:
        raise ValueError("This command only writes isolated historical seasons")
    env = dict(os.environ, BQ_PROJECT=project, BQ_LOCATION=location, NBA_SEASON=season)
    for name, default in [
        ("BQ_DATASET_BRONZE", "nba_bronze"),
        ("BQ_DATASET_SILVER", "nba_silver"),
        ("BQ_DATASET_GOLD", "nba_gold"),
        ("BQ_DATASET_AGENT", "nba_agent"),
        ("BQ_METADATA_DATASET", "nba_metadata"),
    ]:
        env[name] = dataset_for_season(os.environ.get(name, default), season)
    return env


def publish(season: str, frames: dict, report: dict, args) -> None:
    env = warehouse_env(season, args.project, args.location)
    client = bigquery.Client(project=args.project, location=args.location)
    bucket = storage.Client(project=args.project).bucket(args.bucket)
    datasets = {
        key: value
        for key, value in env.items()
        if key.startswith("BQ_DATASET_") or key == "BQ_METADATA_DATASET"
    }
    report["datasets"] = datasets
    for dataset in datasets.values():
        candidate = bigquery.Dataset(f"{args.project}.{dataset}")
        candidate.location = args.location
        client.create_dataset(candidate, exists_ok=True)
    report["inputs"] = {}
    for domain, frame in frames.items():
        table_name, schema_fn = TABLES[domain]
        schema = schema_fn()
        payload = frame.to_csv(index=False).encode()
        digest = hashlib.sha256(payload).hexdigest()
        name = f"nba_data/{season}/historical_backfill/sha256={digest}/{domain}.csv"
        blob = bucket.blob(name)
        if not blob.exists():
            blob.upload_from_string(
                payload, content_type="text/csv", if_generation_match=0
            )
        blob.reload()
        report["inputs"][domain] = {
            "gcs_uri": f"gs://{args.bucket}/{name}",
            "generation": blob.generation,
            "sha256": digest,
            "rows": len(frame),
        }
        table_id = f"{args.project}.{env['BQ_DATASET_BRONZE']}.{table_name}"
        fingerprint = "historical_backfill_sha256=" + digest
        try:
            existing = client.get_table(table_id)
        except NotFound:
            existing = None
        replace_table = False
        if existing is not None:
            if existing.description != fingerprint or existing.num_rows not in (
                0,
                len(frame),
            ):
                if not args.replace_owned_archive or not (
                    existing.description or ""
                ).startswith("historical_backfill_sha256="):
                    raise ValueError(
                        f"Refusing to replace unmatched existing archive table: {table_id}"
                    )
                if frame.empty:
                    raise ValueError(f"Refusing an empty replacement for {table_id}")
                backup = (
                    table_id
                    + "__before_backfill_"
                    + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
                )
                client.copy_table(
                    table_id,
                    backup,
                    job_config=bigquery.CopyJobConfig(write_disposition="WRITE_EMPTY"),
                ).result(timeout=180)
                report.setdefault("backups", []).append(
                    {
                        "table": table_id,
                        "backup": backup,
                        "previous_fingerprint": existing.description,
                        "previous_rows": existing.num_rows,
                    }
                )
                replace_table = True
            elif existing.num_rows == len(frame):
                continue
        else:
            table = bigquery.Table(table_id, schema=schema)
            table.description = fingerprint
            client.create_table(table)
        if not frame.empty:
            frame = frame.copy()
            for field in schema:
                if field.field_type in ("DATE", "DATETIME", "TIMESTAMP"):
                    frame[field.name] = pd.to_datetime(
                        frame[field.name], utc=field.field_type == "TIMESTAMP"
                    )
                    if field.field_type == "DATE":
                        frame[field.name] = frame[field.name].dt.date
            job = client.load_table_from_dataframe(
                frame,
                table_id,
                job_config=bigquery.LoadJobConfig(
                    schema=schema,
                    write_disposition="WRITE_TRUNCATE"
                    if replace_table
                    else "WRITE_EMPTY",
                ),
            )
            job.result(timeout=180)
            published_table = client.get_table(table_id)
            published_table.description = fingerprint
            client.update_table(published_table, ["description"])
        print(f"Loaded {table_id}: {len(frame)}", flush=True)
    variables = json.dumps(
        {"nba_season": season, "nba_as_of_date": report["as_of_date"]}
    )
    # Bound API job creation as well as execution; a stalled HTTP submission
    # must not leave a backfill client waiting indefinitely.
    profile = yaml.safe_load((ROOT / "dbt/profiles/profiles.yml").read_text())
    profile["nba_warehouse"]["outputs"]["dev"].pop("timeout_seconds", None)
    profile["nba_warehouse"]["outputs"]["dev"].update(
        job_creation_timeout_seconds=30,
        job_execution_timeout_seconds=300,
        job_retry_deadline_seconds=60,
    )
    with tempfile.TemporaryDirectory() as profiles_dir:
        (Path(profiles_dir) / "profiles.yml").write_text(yaml.safe_dump(profile))
        subprocess.run(
            [
                args.dbt,
                "build",
                "--project-dir",
                str(ROOT),
                "--profiles-dir",
                profiles_dir,
                "--target",
                "dev",
                "--vars",
                variables,
            ],
            env=env,
            check=True,
            timeout=600,
        )
    # Publish the same tested similarity model used by the scheduled pipeline.
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/backfill_similarity_projection.py"),
            "--season",
            season,
            "--write",
        ],
        env=env,
        check=True,
    )
    report["status"] = "published"
    report["published_at_utc"] = datetime.now(UTC).isoformat()
    manifest_table = (
        f"{args.project}.{env['BQ_METADATA_DATASET']}.historical_backfill_manifest"
    )
    client.load_table_from_json(
        [
            {
                "season": season,
                "published_at_utc": report["published_at_utc"],
                "as_of_date": report["as_of_date"],
                "report_json": json.dumps(report, default=str),
            }
        ],
        manifest_table,
        job_config=bigquery.LoadJobConfig(
            schema=[
                bigquery.SchemaField("season", "STRING"),
                bigquery.SchemaField("published_at_utc", "TIMESTAMP"),
                bigquery.SchemaField("as_of_date", "DATE"),
                bigquery.SchemaField("report_json", "STRING"),
            ],
            write_disposition="WRITE_TRUNCATE",
        ),
    ).result(timeout=90)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seasons",
        nargs="+",
        choices=HISTORICAL_SEASONS,
        default=list(HISTORICAL_SEASONS),
    )
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--skip-extract", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument(
        "--replace-owned-archive",
        action="store_true",
        help="Back up and replace only this command's existing historical tables with newly validated inputs.",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("BQ_PROJECT", os.environ.get("GCP_PROJECT_ID")),
    )
    parser.add_argument("--bucket", default=os.environ.get("GCS_BUCKET_NAME"))
    parser.add_argument("--location", default="US")
    parser.add_argument("--dbt", default=str(Path(sys.executable).with_name("dbt")))
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    if args.publish and (not args.project or not args.bucket):
        parser.error("--publish requires --project and --bucket")
    prepared = []
    for season in args.seasons:
        if not args.skip_extract:
            extract(season, args.directory)
            extract_injuries(season, args.directory)
        previous_report = args.directory / f"{season}_report.json"
        if previous_report.exists():
            history = args.directory / "report_history"
            history.mkdir(exist_ok=True)
            (
                history
                / f"{season}_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}.json"
            ).write_text(previous_report.read_text())
        frames, report = prepare(season, args.directory)
        report["status"] = "validated"
        (args.directory / f"{season}_report.json").write_text(
            json.dumps(report, indent=2, default=str)
        )
        prepared.append((season, frames, report))
        print(
            json.dumps(
                {
                    "season": season,
                    "status": "validated",
                    "row_counts": report["row_counts"],
                }
            ),
            flush=True,
        )
    if args.publish:
        for season, frames, report in prepared:
            try:
                publish(season, frames, report, args)
            except Exception as exc:
                report["status"] = "failed"
                report["error"] = str(exc)
                raise
            finally:
                (args.directory / f"{season}_report.json").write_text(
                    json.dumps(report, indent=2, default=str)
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
