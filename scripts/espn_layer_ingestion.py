#!/usr/bin/env python3
"""Manual dev ingestion with independent layers and correction-window discovery."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402
from scripts import espn_sources as source  # noqa: E402
from scripts.espn_corrections import LAYERS, normalize, plan, timestamp  # noqa: E402
from scripts.espn_layer_store import ESPNLayerStore  # noqa: E402
from scripts.espn_only_ingestion import read_capture  # noqa: E402
from scripts.fetch_gamebooks import write_json  # noqa: E402
from scripts.gamebook_cloud import CloudStore  # noqa: E402
from scripts.gamebook_ingestion import (  # noqa: E402
    QueryCheckpoint,
    UncertainJob,
    archive_sources,
)
from scripts.gamebook_sources import (  # noqa: E402
    SourceUnavailable,
    capture_json,
    season_bounds,
)
from scripts.gamebook_store import require  # noqa: E402


def make_store(output, dataset):
    cloud = CloudStore(
        os.environ["GCP_PROJECT_ID"],
        dataset,
        os.environ["GCS_BUCKET_NAME"],
        output,
        os.getenv("BQ_LOCATION", "US"),
    )
    store = ESPNLayerStore(cloud)
    checkpoint = QueryCheckpoint(cloud, "espn-layers-manual")
    checkpoint.recover()
    cloud.query_checkpoint = checkpoint
    store.setup()
    return store


def ingest_game(store, payload, game, evidence, observed_at, output, inject_layer=None):
    """Only accepted, recomputed candidates may publish; errors are per layer."""
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for layer in LAYERS:
        try:
            candidate = normalize(payload, game, layer)
        except (ValueError, KeyError, TypeError) as exc:
            rows.append(store.failure(game, layer, str(exc), evidence, observed_at))
            continue
        write_json(output / f"{layer}-candidate.json", candidate)
        rows.append(
            store.publish(
                candidate, evidence, observed_at, inject_failure=inject_layer == layer
            )
        )
    write_json(output / "layers.json", rows)
    return {"source_game_id": game["source_game_id"], "layers": rows}


def run(store, season, dates, output, session, capture=None):
    require(
        0 < len(dates) <= 31 and len(set(dates)) == len(dates), "Invalid date batch"
    )
    low, high = season_bounds(season)
    require(all(low <= d <= high for d in dates), "Dates outside season")
    known = store.coverage(season)
    results, discovery = [], []
    root = capture or output / "capture"

    def read(directory, url):
        if capture is None:
            capture_json(url, directory, session)
        value = read_capture(directory, url)
        meta = json.loads((directory / "attempt.json").read_text())
        observed = timestamp(meta["finished_at"])
        require(
            observed <= datetime.now(timezone.utc) + timedelta(minutes=5),
            "Future capture timestamp",
        )
        return value, observed

    for day in dates:
        path = root / "sources" / str(day)
        existing = [r for r in known if str(r["game_date"]) == str(day)]
        try:
            payload, _ = read(
                path, source.BASE + "/scoreboard?dates=" + day.strftime("%Y%m%d")
            )
            games = [g for g in source.scoreboard(payload, season, day) if g["final"]]
            require(
                {r["source_game_id"] for r in existing}
                <= {g["source_game_id"] for g in games},
                "Discovery lost previously observed finals",
            )
        except (OSError, SourceUnavailable, ValueError, KeyError, TypeError) as exc:
            evidence = archive_sources(store.cloud, path)
            discovery.append(
                {
                    "date": str(day),
                    "status": "failed",
                    "error": str(exc),
                    "evidence": evidence,
                }
            )
            for row in existing:
                for layer in LAYERS:
                    store.failure(row, layer, "Discovery failed: " + str(exc), evidence)
            continue
        evidence = archive_sources(store.cloud, path)
        discovery.append(
            {
                "date": str(day),
                "status": "captured",
                "finals": len(games),
                "evidence": evidence,
            }
        )
        store.register_games(games)
        for game in games:
            gid = game["source_game_id"]
            path = root / "games" / gid / "source"
            target = output / "games" / gid
            target.mkdir(parents=True, exist_ok=False)
            try:
                payload, observed = read(path, source.BASE + "/summary?event=" + gid)
            except (OSError, SourceUnavailable, ValueError, KeyError, TypeError) as exc:
                evidence = archive_sources(store.cloud, path)
                row = {
                    "source_game_id": gid,
                    "layers": [
                        store.failure(game, layer, str(exc), evidence)
                        for layer in LAYERS
                    ],
                }
                write_json(target / "layers.json", row)
            else:
                evidence = archive_sources(store.cloud, path)
                row = ingest_game(store, payload, game, evidence, observed, target)
            results.append(row)
            print(gid, [(r["layer"], r["status"]) for r in row["layers"]], flush=True)
    report = {
        "dataset": store.prefix,
        "mode": "frozen_capture_replay" if capture else "fresh_capture",
        "discovery": discovery,
        "games": results,
    }
    write_json(output / "result.json", report)
    # Query coherent published statuses after all layers, not individual task success.
    coverage = store.coverage(season)
    (output / "coverage.json").write_text(
        json.dumps(coverage, default=str, indent=2) + "\n"
    )
    archive_sources(store.cloud, output / "games")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", required=True)
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--correction-window", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dataset", default="nba_gamebook_dev_espn_only")
    args = parser.parse_args()
    require(
        args.output.resolve().is_relative_to(ROOT / "reports")
        and not args.output.exists(),
        "Use new private output directory",
    )
    require(
        not args.plan_only or args.correction_window, "Plan requires correction window"
    )
    require(
        not args.capture or args.capture.resolve().is_relative_to(ROOT / "reports"),
        "Capture must be private reports",
    )
    if args.correction_window:
        require(
            not args.capture and not args.start_date and not args.end_date,
            "Correction window requires fresh capture and automatic dates",
        )
    else:
        require(
            args.start_date is not None
            and args.end_date is not None
            and 0 <= (args.end_date - args.start_date).days < 7,
            "Use bounded explicit window",
        )
        low, high = season_bounds(args.season)
        require(low <= args.start_date <= args.end_date <= high, "Dates outside season")
    args.output.mkdir(parents=True)
    load_dotenv(ROOT / ".env")
    try:
        store = make_store(args.output / "cloud", args.dataset)
        if args.correction_window:
            work = plan(
                args.season, datetime.now(timezone.utc), store.coverage(args.season)
            )
            write_json(args.output / "plan.json", work)
            store.cloud.upload(
                (args.output / "plan.json").read_bytes(),
                "espn_correction_plans",
                "json",
            )
            if args.plan_only:
                print(json.dumps(work))
                return 0
            dates = [date.fromisoformat(d) for d in work["dates"]]
            if not dates:
                print("No dates due; inspect plan.json for exceptions")
                return 0
        else:
            dates = [
                args.start_date + timedelta(days=n)
                for n in range((args.end_date - args.start_date).days + 1)
            ]
        with requests.Session() as session:
            report = run(store, args.season, dates, args.output, session, args.capture)
        return (
            2
            if any(d["status"] == "failed" for d in report["discovery"])
            or any(
                r["status"] == "rejected" or r.get("health") == "failed"
                for g in report["games"]
                for r in g["layers"]
            )
            else 0
        )
    except UncertainJob as exc:
        write_json(
            args.output / "failure.json",
            {"type": type(exc).__name__, "error": str(exc)},
        )
        return 3
    except Exception as exc:
        write_json(
            args.output / "failure.json",
            {"type": type(exc).__name__, "error": str(exc)},
        )
        raise


if __name__ == "__main__":
    sys.exit(main())
