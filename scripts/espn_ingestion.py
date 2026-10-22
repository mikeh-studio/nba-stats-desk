#!/usr/bin/env python3
"""Bounded ESPN dev capture/publication; never writes NBA API run metadata."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from scripts import espn_identity as identity  # noqa: E402
from scripts import espn_sources as source  # noqa: E402
from scripts.espn_store import ESPNStore  # noqa: E402
from scripts.fetch_gamebooks import run as fetch_pdf  # noqa: E402
from scripts.fetch_gamebooks import write_json  # noqa: E402
from scripts.gamebook_cloud import CloudStore  # noqa: E402
from scripts.gamebook_ingestion import (  # noqa: E402
    QueryCheckpoint,
    UncertainJob,
    archive_sources,
)
from scripts.gamebook_publication import due_through, publication_report  # noqa: E402
from scripts.gamebook_sources import (  # noqa: E402
    SourceUnavailable,
    capture_json,
    season_bounds,
)
from scripts.gamebook_store import canonical, normalize, require  # noqa: E402
from scripts.parse_gamebooks import parse_document, sha  # noqa: E402


def make_store(output, key):
    cloud = CloudStore(
        os.environ["GCP_PROJECT_ID"],
        os.getenv("ESPN_DEV_DATASET", "nba_gamebook_dev_espn"),
        os.environ["GCS_BUCKET_NAME"],
        output,
        os.getenv("BQ_LOCATION", "US"),
    )
    store = ESPNStore(cloud)
    checkpoint = QueryCheckpoint(cloud, "espn-" + key)
    checkpoint.recover()
    cloud.query_checkpoint = checkpoint
    store.setup()
    return store


def discover(
    output,
    season,
    first,
    last,
    store,
    session,
    limit=20,
    mapping_capture=None,
    *,
    as_of=None,
):
    start, end = season_bounds(season)
    as_of = as_of or datetime.now(ZoneInfo("America/New_York"))
    cutoff = due_through(as_of)
    today = as_of.astimezone(ZoneInfo("America/New_York")).date()
    require(
        start <= first <= last <= min(end, today) and (last - first).days < 7,
        "Discovery requires a past/current window of at most seven days",
    )
    require(1 <= limit <= 50, "Invalid ESPN batch bound")
    directory = output / "sources"
    directory.mkdir()
    games = []
    mapping = []
    mapping_error = None
    try:
        for offset in range((last - first).days + 1):
            day = first + timedelta(days=offset)
            payload = capture_json(
                source.BASE
                + "/scoreboard?"
                + urlencode({"dates": day.strftime("%Y%m%d"), "limit": 100}),
                directory / day.isoformat(),
                session,
            )
            games.extend(source.scoreboard(payload, season, day))
        require(
            len({g["source_game_id"] for g in games}) == len(games),
            "Duplicate game across dates",
        )
        try:
            if mapping_capture is not None:
                meta = json.loads((mapping_capture / "attempt.json").read_text())
                body = (mapping_capture / "response.body").read_bytes()
                expected = "https://api.pbpstats.com/get-games/nba?" + urlencode(
                    {"Season": season, "SeasonType": "Regular Season"}
                )
                require(
                    meta["status"] == "captured"
                    and meta["http_status"] == 200
                    and meta["url"] == expected
                    and sha(body) == meta["sha256"],
                    "Invalid frozen mapping capture",
                )
                shutil.copytree(mapping_capture, directory / "nba_mapping")
                payload = json.loads(body)
            else:
                payload = capture_json(
                    "https://api.pbpstats.com/get-games/nba?"
                    + urlencode({"Season": season, "SeasonType": "Regular Season"}),
                    directory / "nba_mapping",
                    session,
                )
            mapping = payload["results"]
            require(
                isinstance(mapping, list) and len(mapping) <= 1500,
                "Invalid mapping list",
            )
        except (SourceUnavailable, ValueError, KeyError, TypeError) as exc:
            mapping_error = str(exc)
            mapping = []
        write_json(
            directory / "mapping_status.json",
            {"error": mapping_error, "records": len(mapping)},
        )
    finally:
        evidence = archive_sources(store.cloud, directory)
    finals = [g for g in games if g["final"]]
    previous = store.cloud.query(
        f"SELECT c.source_game_id,c.game_date,a.finished_at FROM `{store.prefix}.espn_coverage` c LEFT JOIN `{store.prefix}.espn_attempts` a ON a.attempt_id=c.last_attempt_id WHERE c.season=@season",
        [store.cloud.parameter("season", season)],
    )
    expected = {
        r["source_game_id"]
        for r in previous
        if str(first) <= str(r["game_date"]) <= str(last)
    }
    require(
        expected <= {g["source_game_id"] for g in finals},
        "Discovery lost previously registered finals",
    )
    # Register the complete observed final-game denominator before capping work.
    store.register_games(finals)
    checked = {r["source_game_id"]: r["finished_at"] for r in previous}
    cached = store.mappings(season)
    require(len(cached) <= 1500, "Mapping cache bound exceeded")
    write_json(output / "mapping_cache.json", cached)
    cache_evidence = store.cloud.upload(
        canonical(cached).encode(), "espn_mapping_cache_observations", "json"
    )
    candidates = []
    for game in sorted(
        finals,
        key=lambda g: (
            str(checked.get(g["source_game_id"]) or ""),
            g["date"],
            g["source_game_id"],
        ),
    ):
        error = None
        try:
            nba_id = source.game_mapping(game, mapping)
            if nba_id:
                record = next(r for r in mapping if r["GameId"] == nba_id)
                store.remember_mapping(game, nba_id, record, evidence)
            else:
                cached_rows = [
                    r for r in cached if r["source_game_id"] == game["source_game_id"]
                ]
                require(len(cached_rows) <= 1, "Ambiguous cached mapping")
                nba_id = source.game_mapping(
                    game, [json.loads(r["record"]) for r in cached_rows]
                )
        except (ValueError, KeyError, TypeError) as exc:
            nba_id = None
            error = str(exc)
        candidates.append(
            {
                "game": game,
                "nba_game_id": nba_id,
                "mapping_error": error,
                "mapping_source_status": mapping_error,
                "discovery_evidence": evidence,
                "mapping_cache_evidence": cache_evidence,
            }
        )
    publication = publication_report(finals, as_of, "source_game_id")
    eligible = [
        c for c in candidates if date.fromisoformat(c["game"]["date"]) <= cutoff
    ]
    selected = eligible[:limit]
    report = {
        "provider": "espn",
        "season": season,
        "start_date": str(first),
        "end_date": str(last),
        "observed_games": len(games),
        "final_games": len(finals),
        "unfinished_games": len(games) - len(finals),
        "selected": len(selected),
        "deferred": len(finals) - len(selected),
        "deferred_by_limit": len(eligible) - len(selected),
        **publication,
        "mapped_games": sum(bool(c["nba_game_id"]) for c in candidates),
        "mapping_source_error": mapping_error,
        "coverage_scope": "ESPN observed finals in explicit date window; NBA mappings may lag",
        "evidence": evidence,
    }
    write_json(output / "discovery.json", report)
    store.cloud.upload(canonical(report).encode(), "espn_discoveries", "json")
    return selected


def captured_pdf(capture, destination, game):
    """Replay explicitly selected evidence without claiming a fresh download."""
    attempt = json.loads((capture / "attempt.json").read_text())
    digest = attempt.get("sha256", "")
    require(
        len(digest) == 64 and all(c in "0123456789abcdef" for c in digest),
        "Invalid captured PDF digest",
    )
    artifact = digest + ".pdf"
    require(
        attempt.get("status") == "available"
        and attempt.get("http_status") == 200
        and attempt.get("game") == game
        and attempt.get("artifact") == artifact
        and attempt.get("url")
        == f"https://statsdmz.nba.com/pdfs/{game['date'].replace('-', '')}/{game['date'].replace('-', '')}_{game['away']}{game['home']}_book.pdf",
        "Captured PDF scope/provenance differs",
    )
    body = (capture / artifact).read_bytes()
    require(sha(body) == digest, "Captured PDF hash differs")
    destination.mkdir(parents=True)
    (destination / artifact).write_bytes(body)
    write_json(destination / "attempt.json", attempt)
    return attempt, destination / artifact


def process(entry, output, store, session, pdf_capture=None):
    game = entry["game"]
    store.register(game)
    directory = output / "sources"
    directory.mkdir()
    candidate = None
    player_mappings = []
    error = None
    try:
        payload = capture_json(
            source.BASE
            + "/summary?"
            + urlencode({"event": source.identifier(game["source_game_id"])}),
            directory / "summary",
            session,
        )
        reference, pin = identity.fetch_catalog(directory / "player_catalog", session)
        write_json(
            directory / "nba_player_reference.json",
            {
                "source": "pinned_nba_api_catalog",
                "pin": pin,
                "players": reference,
            },
        )
        candidate = source.normalize_summary(payload, game, reference)
        player_mappings, _ = identity.crosswalk(candidate["players"], reference, pin)
        candidate.update(
            nba_game_id=entry["nba_game_id"],
            player_mapping_method=pin["method"],
            pdf_validation={"status": "pending_mapping", "matches": []},
        )
        require(not entry.get("mapping_error"), entry.get("mapping_error"))
        if candidate["nba_game_id"] and all(
            p["nba_player_id"] for p in candidate["players"] if p["played"]
        ):
            pdf_game = source.pdf_game(candidate)
            if pdf_capture is not None:
                attempt, pdf_path = captured_pdf(
                    pdf_capture, directory / "pdf" / pdf_game["game_id"], pdf_game
                )
                write_json(
                    directory / "pdf_replay.json",
                    {
                        "mode": "explicit_frozen_capture",
                        "original_finished_at": attempt["finished_at"],
                    },
                )
            else:
                report = fetch_pdf(
                    [pdf_game], directory / "pdf", "espn-dev-worker", 20, session
                )
                attempt = report["attempts"][0]
                if attempt["status"] != "available":
                    raise SourceUnavailable(f"PDF retrieval: {attempt['status']}")
                pdf_path = directory / "pdf" / pdf_game["game_id"] / attempt["artifact"]
            identity_path = directory / "identity.json"
            write_json(identity_path, source.pdf_identity(candidate))
            parsed = parse_document(pdf_path, attempt, identity_path)
            # Apply the established full PDF storage validator before comparison.
            normalize(parsed, pdf_path.read_bytes(), identity_path.read_bytes())
            write_json(directory / "pdf_extraction.json", parsed)
            matches = source.compare_pdf(candidate, parsed)
            candidate["pdf_validation"] = {
                "status": "matched",
                "document_sha256": attempt["sha256"],
                "extraction_revision": parsed["extraction"]["revision"],
                "matches": matches,
            }
        write_json(directory / "candidate.json", candidate)
    except (SourceUnavailable, ValueError, KeyError, TypeError, StopIteration) as exc:
        error = {
            "type": type(exc).__name__,
            "message": str(exc),
            "retryable": isinstance(exc, SourceUnavailable),
        }
        write_json(directory / "failure.json", error)
    finally:
        write_json(directory / "discovery_reference.json", entry)
        evidence = archive_sources(store.cloud, directory)
    if player_mappings:
        mapping_result = store.remember_players(player_mappings, evidence)
        if mapping_result["status"] == "rejected":
            error = {"message": mapping_result["error"], "retryable": False}
    if error:
        result = store.failure(game, error["message"], evidence)
        result["retryable"] = error["retryable"]
    else:
        result = store.publish(candidate, evidence)
    write_json(output / "outcome.json", result)
    return result


def outcome_exit_code(result):
    if (
        result["status"] in ("published", "noop")
        and result.get("pdf_validation") == "matched"
        and result.get("health") == "healthy"
    ):
        return 0
    return 1 if result.get("retryable") else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["discover", "process", "refresh-player-mappings"]
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--season")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--game-json")
    parser.add_argument(
        "--pdf-capture",
        type=Path,
        help="Explicit frozen PDF evidence for manual comparison; retains original capture timestamps",
    )
    parser.add_argument(
        "--mapping-capture",
        type=Path,
        help="Explicit replay of a hashed successful PBP Stats capture; never a game-discovery substitute",
    )
    args = parser.parse_args()
    require(
        args.output.resolve().is_relative_to(ROOT / "reports")
        and not args.output.exists(),
        "Output must be new private reports directory",
    )
    args.output.mkdir(parents=True)
    load_dotenv(ROOT / ".env")
    try:
        today = datetime.now(ZoneInfo("America/New_York")).date()
        if args.command == "discover":
            first = (
                date.fromisoformat(args.start_date)
                if args.start_date
                else today - timedelta(days=6)
            )
            last = date.fromisoformat(args.end_date) if args.end_date else today
            season_bounds(args.season or "")
            job_key = "discovery"
        elif args.command == "refresh-player-mappings":
            season_bounds(args.season or "")
            job_key = "player-mappings"
        else:
            entry = json.loads(args.game_json)
            job_key = source.identifier(entry["game"]["source_game_id"])
        store = make_store(args.output / "cloud", job_key)
        with requests.Session() as session:
            if args.command == "discover":
                result = discover(
                    args.output,
                    args.season,
                    first,
                    last,
                    store,
                    session,
                    int(os.getenv("ESPN_MAX_GAMES_PER_RUN", "20")),
                    mapping_capture=args.mapping_capture,
                )
                code = 0
            elif args.command == "refresh-player-mappings":
                result = identity.refresh(store, args.season, args.output, session)
                code = (
                    0
                    if result["status"] in ("published", "noop")
                    and not result["unresolved"]
                    else 2
                )
            else:
                result = process(entry, args.output, store, session, args.pdf_capture)
                code = outcome_exit_code(result)
            write_json(args.output / "result.json", result)
            return code
    except UncertainJob as exc:
        write_json(
            args.output / "failure.json",
            {"error": str(exc), "type": type(exc).__name__},
        )
        return 3
    except Exception as exc:
        write_json(
            args.output / "failure.json",
            {"error": str(exc), "type": type(exc).__name__},
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
