#!/usr/bin/env python3
"""Bounded, local NBA gamebook retrieval; artifacts belong under ignored reports/."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import requests
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 20 * 1024 * 1024
MAX_GAMES = 10


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def load_manifest(path):
    games = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(games, list) or not 1 <= len(games) <= MAX_GAMES:
        raise ValueError(f"Manifest must contain 1–{MAX_GAMES} games")
    seen = set()
    for game in games:
        if not isinstance(game, dict):
            raise ValueError("Each game must be an object")
        for key in (
            "game_id",
            "date",
            "away",
            "home",
            "away_name",
            "home_name",
            "season",
        ):
            if not isinstance(game.get(key), str) or not game[key].strip():
                raise ValueError(f"Missing string field: {key}")
        if not re.fullmatch(r"\d{10}", game["game_id"]) or game["game_id"] in seen:
            raise ValueError("Game IDs must be unique ten-digit strings")
        seen.add(game["game_id"])
        day = date.fromisoformat(game["date"])
        if not re.fullmatch(r"\d{4}-\d{2}", game["season"]):
            raise ValueError("Season must be YYYY-YY")
        start = int(game["season"][:4])
        if game["season"][-2:] != str(start + 1)[-2:] or not (
            date(start, 7, 1) <= day < date(start + 1, 7, 1)
        ):
            raise ValueError("Date and season disagree")
        if any(not re.fullmatch(r"[A-Z]{3}", game[k]) for k in ("away", "home")):
            raise ValueError("Team abbreviations must have three uppercase letters")
        if game["away"] == game["home"]:
            raise ValueError("Home and away must differ")
    return games


def source_url(game):
    stamp = game["date"].replace("-", "")
    return (
        f"https://statsdmz.nba.com/pdfs/{stamp}/"
        f"{stamp}_{game['away']}{game['home']}_book.pdf"
    )


def normalized(text):
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def inspect_pdf(body, game):
    """Validate basic readability and date/team evidence, not event completeness."""
    result = {"pdf_valid": False, "identity_verified": False}
    if not body.startswith(b"%PDF-"):
        return {**result, "validation_error": "not_pdf"}
    try:
        reader = PdfReader(io.BytesIO(body))
        if reader.is_encrypted:
            return {**result, "validation_error": "encrypted_pdf"}
        texts = [page.extract_text() or "" for page in reader.pages]
        if not texts:
            return {**result, "validation_error": "empty_pdf"}
    except Exception as exc:
        return {
            **result,
            "validation_error": "unreadable_pdf",
            "error_type": type(exc).__name__,
        }
    header = normalized(texts[0])
    day = date.fromisoformat(game["date"])
    expected_date = normalized(f"{day.strftime('%B')} {day.day} {day.year}")
    checks = {
        "date": expected_date in header,
        "away_team": normalized(game["away_name"]) in header,
        "home_team": normalized(game["home_name"]) in header,
    }
    return {
        "pdf_valid": True,
        "page_count": len(texts),
        "text_pages": sum(bool(t.strip()) for t in texts),
        "pbp_marker_pages": [
            i + 1 for i, text in enumerate(texts) if "play by play" in normalized(text)
        ],
        "identity_checks": checks,
        "identity_verified": all(checks.values()),
        "identity_method": "first_page_date_and_team_names; game_id_from_manifest",
        "validation_scope": "readability_and_identity_only; event_completeness_not_checked",
    }


def fetch_one(game, directory, session, timeout):
    started = time.monotonic()
    record = {
        "game": game,
        "url": source_url(game),
        "started_at": now(),
        "http_status": None,
        "status": "network_error",
        "sha256": None,
        "size_bytes": 0,
        "body_complete": False,
    }
    body = bytearray()
    limit_error = None
    try:
        # No automatic retries or redirect following: record the first response.
        with session.get(
            record["url"],
            timeout=(timeout, timeout),
            stream=True,
            allow_redirects=False,
        ) as response:
            record.update(
                http_status=response.status_code,
                content_type=response.headers.get("Content-Type"),
                content_length=response.headers.get("Content-Length"),
            )
            for chunk in response.iter_content(chunk_size=65536):
                if len(body) + len(chunk) > MAX_BYTES:
                    body.extend(chunk[: MAX_BYTES - len(body)])
                    limit_error = "size_limit"
                    break
                body.extend(chunk)
                if time.monotonic() - started > 2 * timeout:
                    limit_error = "duration_limit"
                    break
            record["body_complete"] = limit_error is None
            code = response.status_code
            if code in (401, 403, 429):
                record["status"] = "http_blocked"
            elif code == 404:
                record["status"] = "not_found"
            elif code != 200:
                record["status"] = "http_error"
            elif limit_error:
                record["status"] = limit_error
            else:
                record.update(inspect_pdf(bytes(body), game))
                if not record["pdf_valid"]:
                    record["status"] = "invalid_pdf"
                elif not record["identity_verified"]:
                    record["status"] = "identity_unverified"
                elif not record["pbp_marker_pages"]:
                    record["status"] = "pbp_unverified"
                else:
                    record["status"] = "available"
            if limit_error:
                record["capture_limit"] = limit_error
    except requests.RequestException as exc:
        record.update(status="network_error", error_type=type(exc).__name__)
        # Persist any partial body but never mistake it for a valid artifact.
    if body or record["http_status"] is not None:
        payload = bytes(body)
        suffix = ".pdf" if record.get("pdf_valid") else ".body"
        name = digest(payload) + suffix
        with (directory / name).open("xb") as stream:
            stream.write(payload)
        record.update(sha256=digest(payload), size_bytes=len(payload), artifact=name)
    record.update(
        finished_at=now(), elapsed_seconds=round(time.monotonic() - started, 3)
    )
    write_json(directory / "attempt.json", record)
    return record


def run(games, output, runtime_label, timeout, session):
    output.mkdir(parents=True, exist_ok=False)
    manifest_bytes = (json.dumps(games, indent=2) + "\n").encode()
    (output / "manifest.json").write_bytes(manifest_bytes)
    attempts = []
    for game in games:
        directory = output / game["game_id"]
        directory.mkdir()
        result = fetch_one(game, directory, session, timeout)
        attempts.append(result)
        print(
            f"{game['game_id']} {game['away']}–{game['home']}: "
            f"{result['status']} (HTTP {result['http_status']}, "
            f"{result['size_bytes']} bytes)",
            flush=True,
        )
    report = {
        "schema_version": "gamebook_access/1",
        "runtime_label": runtime_label,
        "runtime_note": "Caller-supplied label, not proof of production access",
        "manifest_sha256": digest(manifest_bytes),
        "completed_at": now(),
        "expected_games": len(games),
        "available_games": sum(a["status"] == "available" for a in attempts),
        "attempts": attempts,
    }
    write_json(output / "report.json", report)
    rows = [
        "# Gamebook retrieval report",
        "",
        f"Runtime: {runtime_label}",
        "",
        "Available means readable PDF, date/team identity and PBP markers; "
        "event completeness has not been validated.",
        "",
        "| Game | Date | Matchup | Status | HTTP | Bytes | Pages |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for a in attempts:
        g = a["game"]
        rows.append(
            f"| {g['game_id']} | {g['date']} | {g['away']} at {g['home']} | "
            f"{a['status']} | {a['http_status']} | {a['size_bytes']} | "
            f"{a.get('page_count', '—')} |"
        )
    (output / "report.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--runtime-label", default="local-cli")
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()
    if not 0 < args.timeout <= 60:
        parser.error("timeout must be greater than zero and at most 60 seconds")
    try:
        games = load_manifest(args.manifest)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    output = args.output or ROOT / "reports" / "gamebook-pilot" / (
        datetime.now(timezone.utc).strftime("access-%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
    )
    if not output.resolve().is_relative_to((ROOT / "reports").resolve()):
        parser.error("output must be inside ignored reports/ to keep captures private")
    if output.exists():
        parser.error("output already exists; choose a new attempt directory")
    with requests.Session() as session:
        session.headers.update(
            {"User-Agent": "NBA-gamebook-access-pilot/1.0", "Accept": "application/pdf"}
        )
        report = run(games, output, args.runtime_label, args.timeout, session)
    print(f"Report: {output / 'report.md'}")
    return 0 if report["available_games"] == report["expected_games"] else 1


if __name__ == "__main__":
    sys.exit(main())
