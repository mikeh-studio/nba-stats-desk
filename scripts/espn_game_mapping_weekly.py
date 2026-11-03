"""Bounded weekly alias discovery; never changes ESPN facts or certification."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.espn_game_mapping_recovery import (  # noqa: E402
    DATASET,
    plan,
    publication_sql,
)
from scripts.gamebook_sources import SourceUnavailable, capture_json  # noqa: E402
from scripts.gamebook_store import canonical, require  # noqa: E402


def observation_sql(prefix, *, reviews=True):
    from scripts.gamebook_cloud import target

    project, dataset = prefix.split(".")
    require(dataset == DATASET, "Wrong mapping observation dataset")
    target(project, dataset)
    review = (
        f"""LEFT JOIN (
SELECT * FROM `{prefix}.espn_game_mapping_reviews` WHERE source='pbpstats'
QUALIFY ROW_NUMBER() OVER(PARTITION BY source_game_id,result_revision,box_revision ORDER BY finished_at DESC,review_id DESC)=1
) r ON g.source_game_id=r.source_game_id AND g.result_revision=r.result_revision AND g.box_revision=r.box_revision"""
        if reviews
        else ""
    )
    columns = (
        "r.status review_status,r.detail review_detail,r.finished_at review_time"
        if reviews
        else "NULL review_status,NULL review_detail,NULL review_time"
    )
    return f"""SELECT g.source_game_id,g.game_date,g.home,g.away,g.home_score,g.away_score,g.nba_game_id,g.nba_game_mapping_status,{columns},JSON_VALUE(m.record,'$.method') identity_method
FROM `{prefix}.espn_layer_status` g LEFT JOIN `{prefix}.espn_mappings` m USING(source_game_id) {review}
WHERE g.season=@season AND g.result_status='accepted' AND g.box_status='accepted'
ORDER BY g.game_date,g.source_game_id LIMIT 1501"""


def summarize(rows):
    require(
        len(rows) <= 1500 and len({r["source_game_id"] for r in rows}) == len(rows),
        "Invalid mapping observation",
    )
    counts = {
        s: sum(r["nba_game_mapping_status"] == s for r in rows)
        for s in ("resolved", "unresolved", "conflict")
    }
    reviewed = sum(r.get("review_status") in ("matched", "mismatch") for r in rows)
    discrepancies = []
    for row in rows:
        if row.get("review_status") != "mismatch":
            continue
        try:
            detail = json.loads(row.get("review_detail") or "{}")
        except (ValueError, TypeError):
            detail = {}
        providers = detail.get("provider_records", [])
        provider = (
            providers[0]
            if isinstance(providers, list)
            and len(providers) == 1
            and isinstance(providers[0], dict)
            else {}
        )
        discrepancies.append(
            {
                "source_game_id": row["source_game_id"],
                "nba_game_id": row.get("nba_game_id"),
                "date": str(row["game_date"]),
                "home": row["home"],
                "away": row["away"],
                "home_score": row["home_score"],
                "away_score": row["away_score"],
                "provider_home_score": provider.get("HomePoints"),
                "provider_away_score": provider.get("AwayPoints"),
                "identity_verified_separately": row.get("identity_method")
                == "reviewed_nba_visible_final_v1",
                "reason": "score_conflict"
                if detail.get("error") == "Mapping score conflict"
                else "mapping_conflict",
                "checked_at": str(row["review_time"])
                if row.get("review_time")
                else None,
            }
        )
    return {
        "status": "degraded"
        if discrepancies
        or counts["unresolved"]
        or counts["conflict"]
        or reviewed < len(rows)
        else "ready"
        if rows
        else "unavailable",
        "games": len(rows),
        "reviewed_games": reviewed,
        **counts,
        "discrepancy_count": len(discrepancies),
        "discrepancies": discrepancies[:50],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def discover(store, session, output, season):
    """Fetch one season feed once. Failure retains prior aliases and audit results."""
    from scripts.gamebook_ingestion import archive_sources

    started = time.monotonic()
    store.cloud.query(
        f"CREATE TABLE IF NOT EXISTS `{store.prefix}.espn_game_mapping_reviews` (review_id STRING,source_game_id STRING,result_revision STRING,box_revision STRING,source STRING,status STRING,detail STRING,evidence_uri STRING,finished_at TIMESTAMP)"
    )
    capture = output / "source"
    url = f"https://api.pbpstats.com/get-games/nba?Season={season}&SeasonType=Regular+Season"
    source_status = "captured"
    try:
        payload = capture_json(url, capture, session)
        records = payload["results"]
        require(
            isinstance(records, list) and 0 < len(records) <= 1500,
            "Invalid game feed count",
        )
    except (SourceUnavailable, ValueError, KeyError, TypeError) as exc:
        meta = (
            json.loads((capture / "attempt.json").read_text())
            if (capture / "attempt.json").exists()
            else {}
        )
        source_status = (
            "access_denied"
            if meta.get("http_status") == 403
            else "rate_limited"
            if meta.get("http_status") == 429
            else "unavailable"
            if isinstance(exc, SourceUnavailable)
            else "invalid_payload"
        )
        if meta.get("error_type") in ("ReadTimeout", "ConnectTimeout", "Timeout"):
            source_status = "timeout"
        elif meta.get("http_status") == 200:
            source_status = "invalid_payload"
        if capture.exists():
            archive_sources(store.cloud, capture)
        result = {
            "status": "degraded",
            "source_status": source_status,
            "error_type": type(exc).__name__,
        }
    else:
        # All accepted games, including old unresolved aliases, are eligible. No PBP data is ingested.
        rows = [
            json.loads(json.dumps(r, default=str))
            for r in store.coverage(season)
            if r["result_status"] == r["box_status"] == "accepted"
        ]
        result = {"status": "ready", "source_status": source_status, "inserted": 0}
        if rows:
            review = plan(rows, records, {})
            source_evidence = archive_sources(store.cloud, capture)
            evidence = store.cloud.upload(
                canonical(
                    {"source_evidence": source_evidence, "review": review}
                ).encode(),
                "weekly_game_mapping",
                "json",
            )
            published = store.cloud.query(
                publication_sql(store.prefix),
                [
                    store.cloud.parameter(k, v)
                    for k, v in {
                        "scope": canonical(rows),
                        "mappings": canonical(review["mappings"]),
                        "checks": canonical(review["checks"]),
                        "evidence": evidence,
                    }.items()
                ],
            )[0]
            result["inserted"] = published["inserted"]
        meta = json.loads((capture / "attempt.json").read_text())
        result["source_observed_at"] = meta["finished_at"]
    # Even after acquisition failure, report actual accepted aliases and prior discrepancies.
    observed = store.cloud.query(
        observation_sql(store.prefix), [store.cloud.parameter("season", season)]
    )
    snapshot = summarize(observed)
    if snapshot["status"] != "ready":
        result["status"] = "degraded"
    result.update(
        snapshot=snapshot, duration_seconds=round(time.monotonic() - started, 3)
    )
    return result


def main():
    from scripts.espn_layer_store import ESPNLayerStore
    from scripts.gamebook_cloud import CloudStore
    from scripts.gamebook_ingestion import QueryCheckpoint

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    require(config.get("source") == "espn", "Explicit ESPN source required")
    # Validate season format before using it in a source URL.
    from scripts.gamebook_sources import season_bounds

    season_bounds(config["season"])
    args.output.mkdir(parents=True, exist_ok=False)
    cloud = CloudStore(
        config["project"], DATASET, config["bucket"], args.output / "cloud"
    )
    checkpoint = QueryCheckpoint(cloud, "weekly-game-mapping")
    checkpoint.recover()
    cloud.query_checkpoint = checkpoint
    store = ESPNLayerStore(cloud)
    store.setup()
    with requests.Session() as session:
        result = discover(store, session, args.output, config["season"])
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    write_source_observation(config, result)
    print(json.dumps({k: v for k, v in result.items() if k != "snapshot"}))


def write_source_observation(config, result):
    from scripts.weekly_refresh import write_json

    state = Path(config["state_dir"])
    require(
        state.is_absolute(), "Mapping observation needs an absolute state directory"
    )
    write_json(
        state / "game-mapping.json",
        {
            "season": config["season"],
            **{
                k: result.get(k)
                for k in (
                    "source_status",
                    "source_observed_at",
                    "inserted",
                    "duration_seconds",
                )
            },
        },
    )


if __name__ == "__main__":
    main()
