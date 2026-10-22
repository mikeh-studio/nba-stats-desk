#!/usr/bin/env python3
"""Manual dev ESPN-only ingestion; optional enrichment never gates valid facts."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402
from scripts import espn_identity as identity  # noqa: E402
from scripts import espn_pbp_probe as probe  # noqa: E402
from scripts import espn_sources as source  # noqa: E402
from scripts.espn_ingestion import captured_pdf  # noqa: E402
from scripts.espn_only_store import ESPNOnlyStore, normalize, validate  # noqa: E402
from scripts.fetch_gamebooks import digest, write_json  # noqa: E402
from scripts.gamebook_cloud import CloudStore, ConfirmedQueryFailure  # noqa: E402
from scripts.gamebook_ingestion import (  # noqa: E402
    QueryCheckpoint,
    UncertainJob,
    archive_sources,
)
from scripts.gamebook_sources import SourceUnavailable, season_bounds  # noqa: E402
from scripts.gamebook_store import normalize as validate_pdf  # noqa: E402
from scripts.gamebook_store import require  # noqa: E402
from scripts.parse_gamebooks import parse_document  # noqa: E402


def make_store(output, dataset):
    cloud = CloudStore(
        os.environ["GCP_PROJECT_ID"],
        dataset,
        os.environ["GCS_BUCKET_NAME"],
        output,
        os.getenv("BQ_LOCATION", "US"),
    )
    store = ESPNOnlyStore(cloud)
    checkpoint = QueryCheckpoint(cloud, "espn-only-manual")
    checkpoint.recover()
    cloud.query_checkpoint = checkpoint
    store.setup()
    return store


def read_capture(directory, url):
    meta = json.loads((directory / "attempt.json").read_text())
    body = (directory / "response.body").read_bytes()
    require(
        len(body) <= 10 * 1024 * 1024 and meta["sha256"] == digest(body),
        "Capture hash/size differs",
    )
    require(meta["url"] == url, "Capture URL differs")
    require(
        meta["status"] == "captured" and meta["http_status"] == 200,
        "Source capture failed",
    )
    return json.loads(body)


def captured_games(capture, season, first, last):
    low, high = season_bounds(season)
    require(
        low <= first <= last <= high and (last - first).days < 7,
        "Invalid discovery window",
    )
    games = []
    for offset in range((last - first).days + 1):
        day = first + timedelta(days=offset)
        payload = read_capture(
            capture / "sources" / str(day),
            source.BASE + "/scoreboard?dates=" + day.strftime("%Y%m%d"),
        )
        games.extend(g for g in source.scoreboard(payload, season, day) if g["final"])
    require(
        len({g["source_game_id"] for g in games}) == len(games),
        "Duplicate discovered games",
    )
    require(bool(games), "No observed finals")
    return games


def verify_pdf(store, candidate, capture, output, reference):
    """Reparse frozen bytes and verify this exact fact revision, not just game ID."""
    enriched = source.normalize_summary(
        candidate["reconciliation_input"], candidate["game"], reference
    )
    accepted_players = store.cloud.query(
        f"SELECT source_player_id,nba_player_id FROM `{store.prefix}.espn_resolved_player_box_scores` WHERE source_game_id=@id AND played",
        [store.cloud.parameter("id", candidate["game"]["source_game_id"])],
    )
    require(
        {p["source_player_id"]: p["nba_player_id"] for p in accepted_players}
        == {
            p["source_player_id"]: p["nba_player_id"]
            for p in enriched["players"]
            if p["played"]
        }
        and all(p["nba_player_id"] for p in accepted_players),
        "PDF reference differs from accepted player crosswalk",
    )
    mappings = store.mappings(candidate["game"]["season"])
    rows = [
        r
        for r in mappings
        if r["source_game_id"] == candidate["game"]["source_game_id"]
    ]
    require(len(rows) == 1, "PDF comparison requires resolved NBA game mapping")
    enriched["nba_game_id"] = source.game_mapping(
        candidate["game"], [json.loads(rows[0]["record"])]
    )
    attempt, path = captured_pdf(capture, output / "pdf", source.pdf_game(enriched))
    identity_path = output / "identity.json"
    write_json(identity_path, source.pdf_identity(enriched))
    parsed = parse_document(path, attempt, identity_path)
    validate_pdf(parsed, path.read_bytes(), identity_path.read_bytes())
    matches = source.compare_pdf(enriched, parsed)
    write_json(
        output / "comparison.json",
        {
            "revision": validate(candidate),
            "mode": "frozen_pdf",
            "matches": matches,
            "original_attempt": attempt,
        },
    )
    return len(matches)


def ingest(store, capture, output, season, first, last, session, pdf_captures=None):
    # Archive original metadata as well as bytes before any registration/publication.
    discovery_evidence = archive_sources(store.cloud, capture / "sources")
    games = captured_games(capture, season, first, last)
    existing = store.cloud.query(
        f"SELECT source_game_id FROM `{store.prefix}.espn_coverage` WHERE season=@season AND game_date BETWEEN @first AND @last",
        [
            store.cloud.parameter("season", season),
            store.cloud.parameter("first", str(first), "DATE"),
            store.cloud.parameter("last", str(last), "DATE"),
        ],
    )
    require(
        {r["source_game_id"] for r in existing} <= {g["source_game_id"] for g in games},
        "Discovery lost previously observed finals",
    )
    store.register_games(games)
    catalog_dir = output / "catalog"
    reference, pin, catalog_error = [], None, None
    try:
        reference, pin = identity.fetch_catalog(catalog_dir, session)
    except (SourceUnavailable, ValueError, KeyError, TypeError) as exc:
        catalog_error = str(exc)
    catalog_evidence = archive_sources(store.cloud, catalog_dir)
    mappings_error = None
    try:
        mappings = store.cloud.query(
            f"SELECT * FROM `{store.cloud.project}.nba_gamebook_dev_espn.espn_mappings` WHERE SUBSTR(nba_game_id,4,2)=@year LIMIT 1501",
            [store.cloud.parameter("year", season[2:4])],
        )
        require(len(mappings) <= 1500, "Mapping bound exceeded")
    except ConfirmedQueryFailure as exc:
        mappings, mappings_error = [], str(exc)
    write_json(
        output / "mapping-source.json", {"rows": mappings, "error": mappings_error}
    )
    mapping_evidence = store.cloud.upload(
        (output / "mapping-source.json").read_bytes(),
        "espn_only_mapping_source",
        "json",
    )
    results = []
    for game in games:
        gid = game["source_game_id"]
        directory = output / "games" / gid
        directory.mkdir(parents=True)
        evidence = archive_sources(store.cloud, capture / "games" / gid / "source")
        row = {"source_game_id": gid, "discovery_evidence": discovery_evidence}
        try:
            payload = read_capture(
                capture / "games" / gid / "source",
                source.BASE + "/summary?event=" + gid,
            )
            candidate = normalize(payload, game)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            row.update(store.failure(game, str(exc), evidence))
            results.append(row)
            write_json(directory / "result.json", row)
            continue
        write_json(directory / "candidate.json", candidate)
        row.update(store.publish(candidate, evidence))
        if row["status"] not in ("published", "noop"):
            results.append(row)
            write_json(directory / "result.json", row)
            continue
        try:
            require(catalog_error is None, catalog_error or "Catalog unavailable")
            active = [p for p in candidate["players"] if p["played"]]
            players, unresolved = identity.crosswalk(active, reference, pin)
            if players:
                publication = store.remember_players(players, catalog_evidence)
                require(
                    publication["status"] in ("published", "noop"),
                    publication.get("error", "Identity publication failed"),
                )
            row["player_mapping"] = "partial" if unresolved else "resolved"
            store.record_check(
                candidate, "player_mapping", row["player_mapping"], catalog_evidence
            )
        except (ValueError, KeyError, TypeError, ConfirmedQueryFailure) as exc:
            row["player_mapping"] = "failed"
            store.record_check(
                candidate, "player_mapping", "failed", catalog_evidence, str(exc)
            )
        try:
            require(
                mappings_error is None, mappings_error or "Game mapping unavailable"
            )
            found = [r for r in mappings if r["source_game_id"] == gid]
            require(len(found) <= 1, "Duplicate mapping")
            if found:
                record = json.loads(found[0]["record"])
                nba_id = source.game_mapping(game, [record])
                require(
                    nba_id == found[0]["nba_game_id"] and found[0]["evidence_uri"],
                    "Mapping scope/evidence differs",
                )
                store.remember_mapping(game, nba_id, record, mapping_evidence)
            row["game_mapping"] = "resolved" if found else "unresolved"
            store.record_check(
                candidate, "game_mapping", row["game_mapping"], mapping_evidence
            )
        except (ValueError, KeyError, TypeError, ConfirmedQueryFailure) as exc:
            row["game_mapping"] = "failed"
            store.record_check(
                candidate, "game_mapping", "failed", mapping_evidence, str(exc)
            )
        row["pdf_verification"] = "not_attempted"
        if gid in (pdf_captures or {}):
            pdf_out = directory / "pdf-check"
            pdf_out.mkdir()
            error = None
            try:
                if (
                    catalog_error
                    or row["player_mapping"] != "resolved"
                    or row["game_mapping"] != "resolved"
                ):
                    raise SourceUnavailable(
                        "PDF verification requires resolved game/player mappings"
                    )
                row["pdf_matches"] = verify_pdf(
                    store, candidate, Path(pdf_captures[gid]), pdf_out, reference
                )
                row["pdf_verification"] = "matched"
            except (OSError, SourceUnavailable, ConfirmedQueryFailure) as exc:
                row["pdf_verification"], error = "unavailable", str(exc)
            except (ValueError, KeyError, TypeError) as exc:
                row["pdf_verification"], error = "mismatch", str(exc)
            write_json(
                pdf_out / "outcome.json",
                {"status": row["pdf_verification"], "error": error},
            )
            pdf_evidence = archive_sources(store.cloud, pdf_out)
            store.record_check(
                candidate, "pdf", row["pdf_verification"], pdf_evidence, error
            )
        results.append(row)
        write_json(directory / "result.json", row)
        print(
            gid, row["status"], row["game_mapping"], row["pdf_verification"], flush=True
        )
    report = {
        "dataset": store.prefix,
        "capture": str(capture),
        "games": results,
        "observed_final_games": len(games),
        "discovery_evidence": discovery_evidence,
    }
    write_json(output / "result.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", required=True)
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--capture", type=Path, help="Explicit replay of original hashed probe capture"
    )
    parser.add_argument(
        "--pdf-captures",
        type=Path,
        help="Private JSON map of ESPN game IDs to frozen PDF capture directories",
    )
    parser.add_argument("--dataset", default="nba_gamebook_dev_espn_only")
    args = parser.parse_args()
    require(
        args.output.resolve().is_relative_to(ROOT / "reports")
        and not args.output.exists(),
        "Use new private output directory",
    )
    if args.capture:
        require(
            args.capture.resolve().is_relative_to(ROOT / "reports"),
            "Capture must be private local reports",
        )
    low, high = season_bounds(args.season)
    require(
        low <= args.start_date <= args.end_date <= high
        and (args.end_date - args.start_date).days < 7,
        "Invalid bounded window",
    )
    args.output.mkdir(parents=True)
    load_dotenv(ROOT / ".env")
    try:
        with requests.Session() as session:
            capture = args.capture or args.output / "capture"
            if not args.capture:
                probe.run(args.season, args.start_date, args.end_date, capture, session)
            store = make_store(args.output / "cloud", args.dataset)
            report = ingest(
                store,
                capture,
                args.output,
                args.season,
                args.start_date,
                args.end_date,
                session,
                json.loads(args.pdf_captures.read_text())
                if args.pdf_captures
                else None,
            )
            return (
                2
                if any(
                    r["status"] == "rejected"
                    or r.get("health") == "failed"
                    or r.get("player_mapping") == "failed"
                    or r.get("game_mapping") == "failed"
                    or r.get("pdf_verification") in ("unavailable", "mismatch")
                    for r in report["games"]
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
