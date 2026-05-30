#!/usr/bin/env python3
"""Recompute and publish player similarity outputs, including the 3D projection.

Adds/refreshes the ``proj_x/proj_y/proj_z`` projection columns on
``nba_gold.player_similarity_features`` without waiting for the next scheduled
DAG run, by reusing the exact pipeline publish path
(``build_player_similarity_outputs`` + ``write_player_similarity_tables``).

Read-only by default: it prints what it would publish. Pass ``--write`` to
actually publish to BigQuery. Requires working GCP auth and the same ``.env``
values the app/pipeline use (BQ_PROJECT, BQ_DATASET_GOLD, BQ_LOCATION).

    python scripts/backfill_similarity_projection.py            # dry run
    python scripts/backfill_similarity_projection.py --write    # publish
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dags"))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass

from google.cloud import bigquery  # noqa: E402

import nba_pipeline as pipeline  # noqa: E402

DEFAULT_SEASON = "2025-26"
PROJECTION_COLUMNS = ("proj_x", "proj_y", "proj_z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", default=os.getenv("NBA_SEASON", DEFAULT_SEASON))
    parser.add_argument(
        "--cluster-count",
        type=int,
        default=int(os.getenv("NBA_ARCHETYPE_CLUSTERS", "10")),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Publish to BigQuery. Without it, the script only reports a dry run.",
    )
    args = parser.parse_args(argv)

    project = os.getenv("BQ_PROJECT") or os.getenv("GCP_PROJECT_ID")
    gold = os.getenv("BQ_DATASET_GOLD", "nba_gold")
    location = os.getenv("BQ_LOCATION", "US")
    if not project:
        print("BQ_PROJECT / GCP_PROJECT_ID is not set.", file=sys.stderr)
        return 2

    client = bigquery.Client(project=project)
    feature_input_table = f"{project}.{gold}.player_similarity_feature_input"
    features_table = f"{project}.{gold}.player_similarity_features"
    archetypes_table = f"{project}.{gold}.player_archetypes"

    feature_input = client.query(
        f"SELECT * FROM `{feature_input_table}` WHERE season = @season",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("season", "STRING", args.season)
            ]
        ),
    ).to_dataframe()
    if feature_input.empty:
        print(f"No feature rows for season {args.season} in {feature_input_table}.")
        return 1

    outputs = pipeline.build_player_similarity_outputs(
        feature_input, cluster_count=args.cluster_count
    )
    features = outputs["features"]
    present = [column for column in PROJECTION_COLUMNS if column in features.columns]

    print(f"season={args.season} input_rows={len(feature_input)} rows={len(features)}")
    print(f"projection columns present: {present}")
    if present:
        preview = features[["player_id", "player_name", *present]].head(5)
        print(preview.to_string(index=False))

    if not args.write:
        print("\nDry run only. Re-run with --write to publish to BigQuery.")
        return 0

    pipeline.ensure_dataset(client, f"{project}.{gold}", location)
    pipeline.write_player_similarity_tables(
        client,
        features_table_id=features_table,
        archetypes_table_id=archetypes_table,
        features_df=features,
        archetypes_df=outputs["archetypes"],
    )
    print(f"\nPublished {len(features)} rows to {features_table} (incl. {present}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
