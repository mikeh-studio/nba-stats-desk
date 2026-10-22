#!/usr/bin/env python3
"""Build explicit identity-only game maps from frozen name/ID references."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.gamebook_store import require  # noqa: E402
from scripts.parse_gamebooks import (  # noqa: E402
    box_score,
    canonical,
    key,
    page_fragments,
    sha,
)


def build_map(game, players, reference, supplement):
    lookup = defaultdict(dict)
    for row in reference["rows"]:
        lookup[key(row["player_name"])][row["player_id"]] = {
            "source_table": reference["source_table"],
            "source_job_id": reference["job_id"],
            "source_ingested_at": row["ingested_at_utc"],
            "source_name": row["player_name"],
        }
    for row in supplement:
        require(
            re.fullmatch(
                r"https://www\.nba\.com/(?:stats/)?player/"
                + re.escape(row["player_id"])
                + r"(?:/[^\s]*)?",
                row["source_url"],
            )
            is not None,
            "Supplement needs an official player-ID URL",
        )
        require(
            bool(row.get("reviewed_at")) and bool(row.get("reviewer")),
            "Supplement needs review attribution",
        )
        lookup[key(row["name"])][row["player_id"]] = {
            **row,
            "source_kind": "reviewed_official_player_page",
        }
    result = {
        "reference_kind": "identity_only_from_pdf_participants_and_frozen_name_id_sources",
        "season": game["season"],
        "date": game["date"],
        "season_type": "Regular Season",
        "home_team_abbreviation": game["home"],
        "away_team_abbreviation": game["away"],
        "scope_note": "Game date/season and team membership come from the captured gamebook; references supply canonical identity only. This is not a provider game-stats response.",
        "stats": {"Home": {"FullGame": []}, "Away": {"FullGame": []}},
    }
    seen = set()
    for p in players:
        if not p["played"]:
            continue
        ids = lookup[key(p["name"])]
        require(len(ids) == 1, f"Unresolved or ambiguous identity: {p['name']}")
        player_id = next(iter(ids))
        require(
            isinstance(player_id, str) and player_id.isdigit() and player_id != "0",
            "Invalid player ID",
        )
        require(p["team"] in (game["home"], game["away"]), "Player outside game teams")
        require(player_id not in seen, "Duplicate participant identity")
        seen.add(player_id)
        side = "Home" if p["team"] == game["home"] else "Away"
        result["stats"][side]["FullGame"].append(
            {
                "Name": p["name"],
                "EntityId": player_id,
                "identity_evidence": ids[player_id],
            }
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--supplement", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(
        args.output.resolve().is_relative_to((ROOT / "reports").resolve())
        and not args.output.exists(),
        "Output must be new and inside reports/",
    )
    reference = json.loads(args.reference.read_text())
    supplement = json.loads(args.supplement.read_text())
    capture = json.loads((args.capture / "report.json").read_text())
    require(
        sha((args.capture / "manifest.json").read_bytes())
        == capture["manifest_sha256"],
        "Capture manifest hash mismatch",
    )
    args.output.mkdir(parents=True)
    for attempt in capture["attempts"]:
        game = attempt["game"]
        require(attempt["status"] == "available", "Gamebook is unavailable")
        path = args.capture / game["game_id"] / attempt["artifact"]
        require(
            path.resolve().is_relative_to(args.capture.resolve()),
            "Invalid capture path",
        )
        require(sha(path.read_bytes()) == attempt["sha256"], "PDF hash mismatch")
        players, _ = box_score(page_fragments(PdfReader(path).pages[0]), game)
        result = build_map(game, players, reference, supplement)
        body = canonical(result).encode()
        (args.output / f"{game['game_id']}-stats.json").write_bytes(body)
        (args.output / f"{game['game_id']}-stats.meta.json").write_text(
            json.dumps(
                {
                    "reference_kind": result["reference_kind"],
                    "document_sha256": attempt["sha256"],
                    "reference_sha256": sha(args.reference.read_bytes()),
                    "supplement_sha256": sha(args.supplement.read_bytes()),
                    "identity_sha256": sha(body),
                    "reference_path": str(args.reference),
                    "supplement_path": str(args.supplement),
                },
                indent=2,
            )
            + "\n"
        )
        print(
            game["game_id"],
            "mapped",
            sum(len(v["FullGame"]) for v in result["stats"].values()),
            flush=True,
        )


if __name__ == "__main__":
    main()
