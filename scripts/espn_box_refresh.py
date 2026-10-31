#!/usr/bin/env python3
"""Explicit ESPN result/box refresh in isolated storage; never NBA publication."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import espn_identity as identity  # noqa: E402
from scripts import espn_sources as source  # noqa: E402
from scripts.espn_corrections import normalize, timestamp, validate  # noqa: E402
from scripts.espn_coverage import audit, check_players, summary  # noqa: E402
from scripts.espn_layer_store import ESPNLayerStore  # noqa: E402
from scripts.espn_only_ingestion import read_capture  # noqa: E402
from scripts.fetch_gamebooks import write_json  # noqa: E402
from scripts.gamebook_cloud import CloudStore  # noqa: E402
from scripts.gamebook_ingestion import QueryCheckpoint, archive_sources  # noqa: E402
from scripts.gamebook_sources import (  # noqa: E402
    SourceUnavailable,
    capture_json,
    season_bounds,
)
from scripts.gamebook_store import require  # noqa: E402

DATASET = "nba_gamebook_dev_espn_only"


def captured_json(url, root, session, cloud, retries):
    """Retain every failed capture; retry acquisition, never data validation."""
    for attempt in range(1, 4):
        directory = root / f"attempt-{attempt}"
        try:
            capture_json(url, directory, session)
            return read_capture(directory, url), directory
        except SourceUnavailable:
            archive_sources(cloud, directory)
            retries.append({"url": url, "attempt": attempt, "status": "failed"})
            if attempt == 3:
                raise
            time.sleep(2 ** (attempt - 1))


def dates_for_refresh(first_date, through, now):
    """Replay two weeks, coalesce missed runs, and bound one batch to 31 dates."""
    last = now.astimezone(ZoneInfo("America/New_York")).date() - timedelta(days=1)
    first = (
        max(first_date, date.fromisoformat(through) - timedelta(days=14))
        if through
        else first_date
    )
    require(
        0 <= (last - first).days < 31,
        "Refresh exceeds 31 dates; use explicit bounded backfill",
    )
    return first, last


def run(config, first, last, output, *, session=None):
    low, high = season_bounds(config["season"])
    require(
        low <= first <= last <= high and (last - first).days < 31,
        "Invalid ESPN date batch",
    )
    require(not output.exists(), "Use a new evidence directory")
    output.mkdir(parents=True, mode=0o700)
    cloud = CloudStore(
        config["project"], DATASET, config["bucket"], output / "cloud", "US"
    )
    checkpoint = QueryCheckpoint(cloud, "espn-box-weekly")
    checkpoint.recover()
    cloud.query_checkpoint = checkpoint
    store = ESPNLayerStore(cloud)
    store.setup()
    owned = session is None
    session = session or requests.Session()
    report = {
        "provider": "espn",
        "dataset": store.prefix,
        "season": config["season"],
        "first_date": str(first),
        "last_date": str(last),
        "discovery": [],
        "games": [],
        "identity_unresolved": 0,
        "source_retries": [],
        "limitations": [
            "ESPN-reported, internally validated box scores; not independently NBA-verified.",
            "Observed ESPN coverage, not certified league-wide completeness.",
            "This job does not validate play-by-play, assist pairs, deflections or NBA game IDs.",
        ],
    }
    try:
        catalog_dir = output / "catalog"
        reference, pin = identity.fetch_catalog(catalog_dir, session)
        catalog_evidence = archive_sources(cloud, catalog_dir)
        known = store.coverage(config["season"])
        accepted = {g["source_game_id"]: g for g in known}
        for n in range((last - first).days + 1):
            day = first + timedelta(days=n)
            directory = output / "sources" / str(day)
            url = (
                source.BASE
                + "/scoreboard?dates="
                + day.strftime("%Y%m%d")
                + "&limit=1000"
            )
            try:
                payload, _ = captured_json(
                    url, directory, session, cloud, report["source_retries"]
                )
                games = [
                    g
                    for g in source.scoreboard(payload, config["season"], day)
                    if g["final"]
                ]
                require(
                    {
                        r["source_game_id"]
                        for r in known
                        if str(r["game_date"]) == str(day)
                    }
                    <= {g["source_game_id"] for g in games},
                    "Discovery lost previously observed finals",
                )
                report["discovery"].append(
                    {
                        "date": str(day),
                        "status": "captured",
                        "finals": len(games),
                        "game_ids": [g["source_game_id"] for g in games],
                    }
                )
                archive_sources(cloud, directory)
                store.register_games(games)
            except Exception as exc:
                report["discovery"].append(
                    {
                        "date": str(day),
                        "status": "failed",
                        "error_type": type(exc).__name__,
                    }
                )
                raise
            daily_mappings = {}
            day_rows = []
            for game in games:
                gid = game["source_game_id"]
                folder = output / "games" / gid
                capture = folder / "source"
                url = source.BASE + "/summary?event=" + gid
                row = {"source_game_id": gid, "layers": []}
                report["games"].append(row)
                day_rows.append(row)
                payload, successful_capture = captured_json(
                    url, capture, session, cloud, report["source_retries"]
                )
                observed = timestamp(
                    json.loads((successful_capture / "attempt.json").read_text())[
                        "finished_at"
                    ]
                )
                evidence = archive_sources(cloud, capture)
                for layer in ("result", "box"):
                    try:
                        candidate = normalize(payload, game, layer)
                    except (ValueError, KeyError, TypeError) as exc:
                        row["layers"].append(
                            store.failure(game, layer, str(exc), evidence, observed)
                        )
                        raise
                    write_json(folder / (layer + "-candidate.json"), candidate)
                    old = accepted.get(gid, {})
                    if old.get(layer + "_status") == "accepted" and old.get(
                        layer + "_revision"
                    ) == validate(candidate):
                        # Fresh bytes, contract validation and evidence are still
                        # retained. Reuse unchanged healthy facts without writing
                        # another transaction. This does not advance store clocks
                        # or clear failed health; the job report records the check.
                        result = {
                            "layer": layer,
                            "status": "validated_noop",
                            "revision": old[layer + "_revision"],
                            "evidence_uri": evidence,
                        }
                    else:
                        result = store.publish(candidate, evidence, observed)
                    row["layers"].append(result)
                    require(
                        result["status"] in ("published", "noop", "validated_noop")
                        and result.get("health", "healthy") == "healthy",
                        "ESPN layer publication failed",
                    )
                players = [p for p in candidate["players"] if p["played"]]
                mappings, unresolved = identity.crosswalk(players, reference, pin)
                row["player_check"] = check_players(candidate, mappings)
                for mapping in mappings:
                    key = mapping["source_player_id"]
                    require(
                        key not in daily_mappings or daily_mappings[key] == mapping,
                        "Conflicting player mappings in date batch",
                    )
                    daily_mappings[key] = mapping
                row["unresolved_identity_rows"] = len(unresolved)
                report["identity_unresolved"] += len(unresolved)
                print(
                    gid, [(r["layer"], r["status"]) for r in row["layers"]], flush=True
                )
            if daily_mappings:
                result = store.remember_players(
                    list(daily_mappings.values()), catalog_evidence
                )
                require(
                    result["status"] in ("published", "noop"),
                    "Identity publication failed",
                )
            for row in day_rows:
                row["mappings_status"] = "published" if daily_mappings else "noop"
        report["status"] = "success"
        return report
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        try:
            ledger = audit(report, config.get("first_date", str(first)))
            report["coverage_checks"] = summary(ledger)
            write_json(output / "coverage.json", ledger)
        except (ValueError, KeyError, TypeError):
            report["coverage_checks"] = {
                "status": "unavailable",
                "issues": ["coverage_report_invalid"],
            }
        write_json(output / "result.json", report)
        if owned:
            session.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--first", required=True, type=date.fromisoformat)
    p.add_argument("--last", required=True, type=date.fromisoformat)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    config = json.loads(args.config.read_text())
    require(config.get("source") == "espn", "Explicit ESPN configuration required")
    run(config, args.first, args.last, args.output)


if __name__ == "__main__":
    main()
