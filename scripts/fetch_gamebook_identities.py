#!/usr/bin/env python3
"""Capture bounded, game-specific identity references for offline gamebook parsing."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.fetch_gamebooks import load_manifest  # noqa: E402


def validate(reference, game):
    if (
        reference["season"] != game["season"]
        or reference["date"][:10] != game["date"]
        or reference["season_type"] != "Regular Season"
    ):
        raise ValueError("Reference date/season/phase differs")
    for side in ("home", "away"):
        if reference[f"{side}_team_abbreviation"] != game[side]:
            raise ValueError("Reference team differs")
        entries = reference["stats"][side.title()]["FullGame"]
        if not entries or any(
            not str(p.get("EntityId", "")).isdigit() or not p.get("Name")
            for p in entries
        ):
            raise ValueError("Missing canonical player identities")


def capture(game, output):
    gid = game["game_id"]
    url = "https://api.pbpstats.com/get-game-stats?" + urlencode(
        {"GameId": gid, "Type": "Player"}
    )
    record = {
        "game": game,
        "url": url,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "status": "network_error",
        "usage": "identity fields only; provider assist counts are not imported",
    }
    body = bytearray()
    try:
        with requests.get(
            url, timeout=(10, 20), stream=True, allow_redirects=False
        ) as response:
            record["http_status"] = response.status_code
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > 10 * 1024 * 1024:
                    raise ValueError("Reference exceeds 10 MiB")
            record["status"] = (
                "blocked" if response.status_code in (401, 403, 429) else "http_error"
            )
            if response.status_code == 200:
                record["status"] = "invalid_reference"
                data = json.loads(body)
                validate(data, game)
                record["status"] = "available"
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        record["error"] = {"type": type(exc).__name__, "message": str(exc)}
    if body:
        record.update(sha256=hashlib.sha256(body).hexdigest(), bytes=len(body))
        filename = (
            f"{gid}-stats.json"
            if record["status"] == "available"
            else f"{gid}-response.bin"
        )
        (output / filename).write_bytes(body)
        record["artifact"] = filename
    (output / f"{gid}-meta.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (
        not args.output.resolve().is_relative_to((ROOT / "reports").resolve())
        or args.output.exists()
    ):
        parser.error("output must be a new directory inside reports/")
    games = load_manifest(args.manifest)
    args.output.mkdir(parents=True)
    results = []
    for game in games:
        record = capture(game, args.output)
        results.append(record)
        print(game["game_id"], record["status"], flush=True)
    (args.output / "report.json").write_text(json.dumps(results, indent=2) + "\n")
    return int(any(r["status"] != "available" for r in results))


if __name__ == "__main__":
    sys.exit(main())
