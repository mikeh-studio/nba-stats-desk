#!/usr/bin/env python3
"""Unattended dev gamebook worker, invoked by Airflow or a bounded CLI run."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from google.api_core.exceptions import NotFound  # noqa: E402
from scripts import gamebook_sources as sources  # noqa: E402
from scripts import gamebook_warehouse_discovery as warehouse  # noqa: E402
from scripts.fetch_gamebooks import load_manifest, write_json  # noqa: E402
from scripts.fetch_gamebooks import run as fetch  # noqa: E402
from scripts.gamebook_cloud import CloudStore  # noqa: E402
from scripts.gamebook_publication import due_through, publication_report  # noqa: E402
from scripts.gamebook_store import canonical, require  # noqa: E402
from scripts.parse_gamebooks import parse_document  # noqa: E402


class UncertainJob(RuntimeError):
    """Stop immediate retries until the prior query has a known terminal outcome."""


class QueryCheckpoint:
    """Durable per-worker checkpoint. Unfinished BQ queries block the next attempt."""

    def __init__(self, store, key):
        self.store = store
        self.blob = store.bucket.blob(f"{store.prefix}/automation_queries/{key}.json")
        self.generation = 0

    def save(self, job_id):
        options = (
            {}
            if self.store.bucket.iam_configuration.uniform_bucket_level_access_enabled
            else {"predefined_acl": "private"}
        )
        self.blob.upload_from_string(
            canonical({"job_id": job_id, "location": self.store.location}),
            if_generation_match=self.generation,
            timeout=30,
            **options,
        )
        self.generation = int(self.blob.generation)

    def recover(self):
        try:
            self.blob.reload(timeout=30)
        except NotFound:
            return
        self.generation = int(self.blob.generation)
        pending = json.loads(
            self.blob.download_as_bytes(if_generation_match=self.generation, timeout=30)
        )
        if pending["job_id"]:
            try:
                job = self.store.bq.get_job(
                    pending["job_id"], location=pending["location"], timeout=30
                )
            except NotFound as exc:
                # A missing job is not proof the submit never reached BigQuery.
                raise UncertainJob(
                    f"Review missing query {pending['job_id']} before clearing checkpoint"
                ) from exc
            if job.state != "DONE":
                raise UncertainJob(f"Query {pending['job_id']} is still {job.state}")
            self.save(
                None
            )  # Successful and failed terminal jobs are both safe to replay.

    def __call__(self, job_id):
        self.save(job_id)


def make_store(output, key):
    store = CloudStore(
        os.environ["GCP_PROJECT_ID"],
        os.getenv("GAMEBOOK_DEV_DATASET", "nba_gamebook_dev"),
        os.environ["GCS_BUCKET_NAME"],
        output,
        os.getenv("BQ_LOCATION", "US"),
    )
    checkpoint = QueryCheckpoint(store, key)
    checkpoint.recover()
    store.query_checkpoint = checkpoint
    store.setup()
    return store


def archive_sources(store, directory):
    evidence = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            evidence[str(path.relative_to(directory))] = store.upload(
                path.read_bytes(), "automation_sources", path.suffix[1:] or "bin"
            )
    return store.upload(canonical(evidence).encode(), "automation_evidence", "json")


def coverage(store, season):
    return store.query(
        f"""SELECT c.*, r.finished_at AS last_checked, r.status AS last_status
FROM `{store.destination}.gamebook_coverage` c
LEFT JOIN `{store.destination}.gamebook_extraction_runs` r ON r.attempt_id=c.last_attempt_id
WHERE c.season=@season AND c.phase='Regular Season'""",
        [store.parameter("season", season)],
    )


def register_completed(store, games):
    """Register the whole discovered denominator, including games beyond this batch."""
    if not games:
        return
    # JSON input remains a bound value, never interpolated source text/identifiers.
    store.query(
        f"""BEGIN TRANSACTION;
UPDATE `{store.destination}.gamebook_store_control` SET generation=generation+1 WHERE store_id='main';
CREATE TEMP TABLE discovered AS SELECT JSON_VALUE(g,'$.game_id') game_id,
CAST(JSON_VALUE(g,'$.date') AS DATE) game_date, JSON_VALUE(g,'$.season') season,
JSON_VALUE(g,'$.away') away, JSON_VALUE(g,'$.home') home
FROM UNNEST(JSON_QUERY_ARRAY(@games)) g;
ASSERT NOT EXISTS(SELECT 1 FROM discovered d JOIN `{store.destination}.gamebook_coverage` c USING(game_id)
WHERE d.game_date!=c.game_date OR d.season!=c.season OR d.away!=c.away OR d.home!=c.home) AS 'scope_changed';
INSERT INTO `{store.destination}.gamebook_coverage` (game_id,source,domain,game_date,season,phase,away,home,health)
SELECT game_id,'nba_gamebook_pdf','assists',game_date,season,'Regular Season',away,home,'pending'
FROM discovered d WHERE NOT EXISTS(SELECT 1 FROM `{store.destination}.gamebook_coverage` c WHERE c.game_id=d.game_id);
COMMIT TRANSACTION;""",
        [store.parameter("games", canonical(games))],
    )


def choose_games(games, states, now, limit=20, correction_days=7):
    require(1 <= limit <= 50 and 1 <= correction_days <= 30, "Invalid queue bounds")
    previous = {s["game_id"]: s for s in states}
    eligible = []
    cutoff = due_through(now)
    for game in games:
        if date.fromisoformat(game["date"]) > cutoff:
            continue
        state = previous.get(game["game_id"], {})
        stamp = state.get("last_checked")
        checked = (
            datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if stamp
            else datetime.min.replace(tzinfo=timezone.utc)
        )
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        age = now - checked
        recent = (now.date() - date.fromisoformat(game["date"])).days < correction_days
        failed = state.get("last_status") not in (None, "published", "noop")
        interval = (
            timedelta(hours=24 if failed else 6)
            if recent or failed or not state.get("current_revision")
            else timedelta(days=7)
        )
        if age < interval:
            continue
        # Never-attempted finals, then a fair oldest-attempt queue (including failures).
        priority = 0 if not stamp else 1
        eligible.append((priority, checked, game["date"], game["game_id"], game))
    eligible.sort(key=lambda row: row[:4])
    return [row[-1] for row in eligible[:limit]], len(eligible)


def run_discovery(
    output,
    season,
    as_of,
    store,
    session,
    limit=20,
    *,
    discovery_source="warehouse",
    mode="daily",
    start_date=None,
    end_date=None,
):
    source = output / "sources"
    source.mkdir()
    try:
        require(
            discovery_source in ("warehouse", "schedule"), "Invalid discovery source"
        )
        if discovery_source == "warehouse":
            games = warehouse.capture(
                store,
                season,
                as_of,
                source / "warehouse",
                bronze_dataset=os.getenv(
                    "GAMEBOOK_SOURCE_BRONZE_DATASET",
                    os.getenv("BQ_DATASET_BRONZE", "nba_bronze"),
                ),
                metadata_dataset=os.getenv(
                    "GAMEBOOK_SOURCE_METADATA_DATASET",
                    os.getenv("BQ_METADATA_DATASET", "nba_metadata"),
                ),
                mode=mode,
                start_date=start_date,
                end_date=end_date,
            )
        else:
            require(
                mode == "daily" and start_date is None and end_date is None,
                "Schedule source does not support backfill mode",
            )
            games = sources.discover(season, as_of.date(), source / "schedule", session)
    finally:
        evidence = archive_sources(store, source)
    states = coverage(store, season)
    if mode == "backfill":
        # Compare membership only inside this explicitly bounded backfill window.
        states = [s for s in states if start_date <= str(s["game_date"]) <= end_date]
    require(
        {s["game_id"] for s in states} <= {g["game_id"] for g in games},
        "Discovery lost previously registered completed games; refusing partial discovery",
    )
    register_completed(store, games)
    selected, due = choose_games(games, states, as_of, limit)
    result = {
        "games": selected,
        "discovered": len(games),
        "due": due,
        "deferred": due - len(selected),
        "season": season,
        "as_of": as_of.isoformat(),
        "evidence": evidence,
        "discovery_source": discovery_source,
        "mode": mode,
        "scope_start": start_date,
        "scope_end": end_date,
        **publication_report(games, as_of, "game_id"),
        "coverage_scope": "completed_games_observed_in_upstream_ingestion"
        if discovery_source == "warehouse"
        else "official_completed_schedule",
    }
    write_json(output / "discovery.json", result)
    store.upload(canonical(result).encode(), "automation_discovery", "json")
    return selected


def process_game(game, output, store, session):
    """One independent attempt; failures are audited before the task raises."""
    store.register([game])
    source = output / "sources"
    source.mkdir()
    retryable, pdf, identity = False, None, None
    result = {
        "game": game,
        "status": "rejected",
        "errors": [],
        "assists": [],
        "scoring_events": [],
        "player_validation": [],
        "box_totals": {},
    }
    try:
        report = fetch([game], source / "pdf", "airflow-dev-worker", 20, session)
        attempt = report["attempts"][0]
        if attempt["status"] != "available":
            # Late PDFs, truncated responses and bad identity headers may be replaced upstream.
            raise sources.SourceUnavailable(f"PDF retrieval: {attempt['status']}")
        pdf_path = source / "pdf" / game["game_id"] / attempt["artifact"]
        pdf = pdf_path.read_bytes()
        identity = sources.identities(game, source / "identities", session)
        result = parse_document(
            pdf_path, attempt, source / "identities" / "identity.json"
        )
    except sources.SourceUnavailable as exc:
        retryable = True
        result["errors"] = [{"error": str(exc), "stage": "source", "retryable": True}]
    except (ValueError, KeyError, TypeError, StopIteration) as exc:
        result["errors"] = [
            {"error": str(exc), "stage": "validation", "retryable": False}
        ]
    finally:
        evidence = archive_sources(store, source)
    outcome = store.ingest(
        result,
        pdf=pdf,
        identity=identity,
        reason=f"automated dev ingestion; evidence={evidence}",
    )
    summary = {
        "game_id": game["game_id"],
        "status": outcome["status"],
        "retryable": retryable or outcome["status"] == "failed",
        "evidence": evidence,
        "attempt_id": outcome["attempt_id"],
    }
    write_json(output / "outcome.json", summary)
    store.upload(canonical(summary).encode(), "automation_outcomes", "json")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("discover", "process"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--season")
    parser.add_argument("--as-of")
    parser.add_argument("--game-json")
    parser.add_argument("--discovery-source", choices=("warehouse", "schedule"))
    parser.add_argument("--mode", choices=("daily", "backfill"), default="daily")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    args = parser.parse_args()
    require(
        args.output.resolve().is_relative_to((ROOT / "reports").resolve())
        and not args.output.exists(),
        "Output must be new and inside private reports/",
    )
    args.output.mkdir(parents=True)
    load_dotenv(ROOT / ".env")
    try:
        if args.command == "discover":
            sources.season_bounds(args.season or "")
            as_of = datetime.fromisoformat(args.as_of)
            require(as_of.tzinfo is not None, "as-of must have timezone")
            key = "discovery"
        else:
            game = json.loads(args.game_json)
            write_json(args.output / "manifest.json", [game])
            load_manifest(args.output / "manifest.json")
            require(
                game["game_id"].startswith("002")
                and game["game_id"][3:5] == game["season"][2:4],
                "Only regular-season games with matching IDs",
            )
            key = game["game_id"]
        store = make_store(args.output / "cloud", key)
        with requests.Session() as session:
            session.headers.update(
                {
                    "User-Agent": "NBA-gamebook-dev-ingestion/1.0",
                    "Accept": "application/json,application/pdf",
                }
            )
            if args.command == "discover":
                selected = run_discovery(
                    args.output,
                    args.season,
                    as_of,
                    store,
                    session,
                    int(os.getenv("GAMEBOOK_MAX_GAMES_PER_RUN", "20")),
                    discovery_source=args.discovery_source
                    or os.getenv("GAMEBOOK_DISCOVERY_SOURCE", "warehouse"),
                    mode=args.mode,
                    start_date=args.start_date,
                    end_date=args.end_date,
                )
                write_json(args.output / "result.json", selected)
                return 0
            result = process_game(game, args.output, store, session)
            write_json(args.output / "result.json", result)
            return (
                0
                if result["status"] in ("published", "noop")
                else 1
                if result["retryable"]
                else 2
            )
    except UncertainJob as exc:
        write_json(
            args.output / "failure.json",
            {"type": type(exc).__name__, "message": str(exc)},
        )
        return 3
    except Exception as exc:
        write_json(
            args.output / "failure.json",
            {"type": type(exc).__name__, "message": str(exc)},
        )
        # Worker/runtime failures must never turn into successful empty results.
        print(
            f"{type(exc).__name__}: inspect {args.output / 'failure.json'}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
