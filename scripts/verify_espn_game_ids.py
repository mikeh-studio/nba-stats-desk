#!/usr/bin/env python3
"""Manually reviewed NBA page + captured ESPN/PDF identity verification in dev."""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import espn_sources as source  # noqa: E402
from scripts.fetch_gamebooks import inspect_pdf, source_url, write_json  # noqa: E402
from scripts.gamebook_store import require  # noqa: E402
from scripts.parse_gamebooks import (  # noqa: E402
    box_score,
    key,
    lines,
    page_fragments,
    sha,
)

METHOD = "reviewed_nba_page_pdf_final_box_v1"


def local_file(root, name):
    path = (root / name).resolve()
    require(
        path.is_relative_to(root.resolve()) and path.is_file(), "Invalid evidence path"
    )
    return path


def captured_summary(directory, source_id):
    meta = json.loads((directory / "attempt.json").read_text())
    body = (directory / "response.body").read_bytes()
    require(
        meta["status"] == "captured"
        and meta["http_status"] == 200
        and meta["url"] == source.BASE + "/summary?event=" + source_id
        and meta["sha256"] == sha(body),
        "Invalid ESPN summary capture",
    )
    return json.loads(body), meta


def verify_page(candidate, page, capture):
    """Operator-reviewed source text, explicitly not a direct HTTP worker capture."""
    day = date.fromisoformat(candidate["date"])
    require(page["method"] == "operator_reviewed_web_tool", "Unsupported page review")
    require(
        datetime.fromisoformat(page["observed_at"]).tzinfo, "Missing review timezone"
    )
    require(sha(capture) == page["sha256"], "Page capture hash mismatch")
    url = page["url"]
    prefix = (
        "https://www.nba.com/game/"
        + candidate["away"].lower()
        + "-vs-"
        + candidate["home"].lower()
        + "-"
        + candidate["nba_game_id_candidate"]
    )
    require(url in (prefix, prefix + "/box-score"), "NBA page URL/scope mismatch")
    title = page["title"]
    expected = (
        source.TEAM_NAMES[candidate["away"]]
        + " vs "
        + source.TEAM_NAMES[candidate["home"]]
        + " "
        + day.strftime("%b")
        + f" {day.day}, {day.year} "
    )
    require(
        title
        in (expected + "Box Scores | NBA.com", expected + "Game Summary | NBA.com"),
        "NBA page title/date/team mismatch",
    )
    text = capture.decode("utf-8")
    require(title + " (" + url + ")" in text, "Title and URL absent from page capture")


def pdf_box_identity(body, game):
    inspected = inspect_pdf(body, game)
    require(
        inspected.get("pdf_valid") and inspected.get("identity_verified"),
        "PDF date/team mismatch",
    )
    page = PdfReader(io.BytesIO(body)).pages[0]
    require("FINAL BOX" in page.extract_text(), "PDF is not a final box score")
    fragments = page_fragments(page)
    for label, side in (("VISITOR:", "away"), ("HOME:", "home")):
        rows = [r for r in lines(fragments) if r[0]["text"] == label]
        require(len(rows) == 1, "Missing/duplicate PDF team heading")
        name = " ".join(f["text"] for f in rows[0][1:])
        name = re.sub(r"\s*\(\d+-\d+\)$", "", name)
        require(key(name) == key(game[side + "_name"]), "PDF home/away role mismatch")
    _, totals = box_score(
        fragments, game
    )  # Reconciles player sums with printed totals.
    return totals


def verify_bundle(candidate, root):
    """Identity only: neither validates PBP assists nor changes fact health."""
    sid = source.identifier(candidate["source_game_id"])
    payload, summary_meta = captured_summary(
        local_file(root, candidate["summary_attempt"]).parent, sid
    )
    game = source.event_game(
        payload["header"], candidate["season"], date.fromisoformat(candidate["date"])
    )
    require(
        game["source_game_id"] == sid and game["final"],
        "ESPN identity is not the expected final",
    )
    require(
        all(game[k] == candidate[k] for k in ("date", "season", "away", "home")),
        "Candidate/ESPN scope mismatch",
    )
    first, last = source.season_bounds(game["season"])
    require(first <= date.fromisoformat(game["date"]) <= last, "Date outside season")
    page = candidate["page"]
    page_bytes = local_file(root, page["capture"]).read_bytes()
    verify_page(candidate, page, page_bytes)
    pdf_path = local_file(root, candidate["pdf_attempt"])
    meta = json.loads(pdf_path.read_text())
    require(
        meta["status"] == "available"
        and meta["http_status"] == 200
        and meta["body_complete"],
        "PDF unavailable: " + meta["status"] + " / " + str(meta.get("error_type")),
    )
    pdf_game = {k: game[k] for k in ("date", "season", "home", "away")}
    pdf_game.update(
        game_id=candidate["nba_game_id_candidate"],
        home_name=source.TEAM_NAMES[game["home"]],
        away_name=source.TEAM_NAMES[game["away"]],
    )
    require(
        meta["game"] == pdf_game and meta["url"] == source_url(pdf_game),
        "PDF capture scope mismatch",
    )
    body = local_file(pdf_path.parent, meta["artifact"]).read_bytes()
    require(sha(body) == meta["sha256"], "PDF hash mismatch")
    totals = pdf_box_identity(body, pdf_game)
    record = {
        "GameId": candidate["nba_game_id_candidate"],
        "Date": game["date"],
        "HomeTeamAbbreviation": game["home"],
        "AwayTeamAbbreviation": game["away"],
        "HomePoints": totals[game["home"]]["PTS"],
        "AwayPoints": totals[game["away"]]["PTS"],
        "method": METHOD,
        "nba_page_url": page["url"],
        "nba_page_capture_sha256": page["sha256"],
        "pdf_sha256": meta["sha256"],
        "espn_summary_sha256": summary_meta["sha256"],
        "scope": "game identity and final score only; game ID from reviewed NBA page, not PDF",
    }
    require(source.game_mapping(game, [record]) == record["GameId"], "Mapping mismatch")
    return {"source_game_id": sid, "status": "verified", "game": game, "record": record}


def verify_all(candidates, root):
    require(0 < len(candidates) <= 20, "Verification batch must contain 1-20 games")
    for field in ("source_game_id", "nba_game_id_candidate"):
        require(
            len({c[field] for c in candidates}) == len(candidates),
            "Duplicate candidate ID",
        )
    results = []
    for candidate in candidates:
        try:
            result = verify_bundle(candidate, root)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            result = {
                "source_game_id": candidate["source_game_id"],
                "status": "rejected",
                "error": str(exc),
            }
        results.append(result)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle",
        type=Path,
        required=True,
        help="Private directory with candidates.json and captured sources",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--publish-dev",
        action="store_true",
        help="Archive evidence and publish verified mappings to the guarded dev store",
    )
    args = parser.parse_args()
    require(
        args.output.resolve().is_relative_to(ROOT / "reports"),
        "Output must remain private under reports/",
    )
    args.output.mkdir(parents=True, exist_ok=False)
    inputs = json.loads((args.bundle / "candidates.json").read_text())
    sources = args.output / "sources"
    sources.mkdir()
    # Copy only the bounded evidence declared by the manifest, never an entire workspace.
    shutil.copyfile(args.bundle / "candidates.json", sources / "candidates.json")
    for c in inputs:
        paths = [
            c["summary_attempt"],
            str(Path(c["summary_attempt"]).with_name("response.body")),
            c["pdf_attempt"],
            c["page"]["capture"],
        ]
        pdf_meta = json.loads(local_file(args.bundle, c["pdf_attempt"]).read_text())
        if pdf_meta.get("artifact"):
            paths.append(str(Path(c["pdf_attempt"]).parent / pdf_meta["artifact"]))
        for name in paths:
            src = local_file(args.bundle, name)
            dst = sources / src.relative_to(args.bundle.resolve())
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
    results = verify_all(inputs, sources)
    write_json(sources / "verification.json", results)
    if args.publish_dev:
        from dotenv import load_dotenv
        from scripts.espn_ingestion import make_store
        from scripts.gamebook_ingestion import archive_sources

        load_dotenv(ROOT / ".env")
        store = make_store(args.output / "cloud", "reviewed-game-ids")
        evidence = archive_sources(store.cloud, sources)
        write_json(args.output / "evidence.json", {"uri": evidence})
        # Existing insert-only transaction protects both ID directions. Unknown
        # query outcomes propagate with the existing durable job checkpoint.
        for result in results:
            if result["status"] == "verified":
                store.remember_mapping(
                    result["game"],
                    result["record"]["GameId"],
                    result["record"],
                    evidence,
                )
                result["publication"] = "mapping_present"
    write_json(args.output / "result.json", results)
    print(
        json.dumps(
            {
                "verified": sum(r["status"] == "verified" for r in results),
                "rejected": sum(r["status"] == "rejected" for r in results),
            }
        )
    )
    return 0 if all(r["status"] == "verified" for r in results) else 2


if __name__ == "__main__":
    sys.exit(main())
