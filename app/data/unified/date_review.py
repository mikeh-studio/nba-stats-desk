"""Reviewed played-date projection over immutable historical archive inputs."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date

from nba_api.stats.static.teams import get_teams

from .identity import canonical, digest, evidence, require, scope

VERSION = "reviewed_nba_played_date/1"
TEAM_NAMES = {team["abbreviation"]: team["full_name"] for team in get_teams()}


def normalize(payload, registry, receipt):
    from .historical import normalize as normalize_archive
    from .store import parse_json

    require(
        isinstance(payload, dict)
        and set(payload)
        == {
            "schema_version",
            "provider",
            "season",
            "phase",
            "game",
            "original_body",
            "original_receipt",
            "review",
        },
        "Date review envelope fields differ",
    )
    require(
        payload["schema_version"] == "nba_archive_date_review/1"
        and payload["provider"] == "nba_api"
        and payload["season"] == "2025-26"
        and payload["phase"] == "Regular Season",
        "Unsupported date review source/scope",
    )
    original_receipt = evidence(payload["original_receipt"])
    require(
        original_receipt["kind"]
        in ("archive_snapshot", "provider_capture", "synthetic_fixture"),
        "Identity evidence cannot supply archive facts",
    )
    require(isinstance(payload["original_body"], str), "Retain original archive bytes")
    original_body = payload["original_body"].encode("utf-8")
    require(
        0 < len(original_body) <= 512_000
        and hashlib.sha256(original_body).hexdigest() == original_receipt["sha256"],
        "Original archive checksum/size differs",
    )
    original = parse_json(original_body)
    require(
        original.get("schema_version") == "nba_archive_game/1"
        and original.get("provider") == payload["provider"]
        and original.get("season") == payload["season"]
        and original.get("phase") == payload["phase"]
        and original["game"] == payload["game"],
        "Original archive identity/scope differs",
    )
    # A date attestation never upgrades unknown participant membership. Complete
    # manifests have their own scope/input bindings and need separate review.
    require(
        "membership" not in original,
        "Participant manifests require separate scope review",
    )
    review = payload["review"]
    require(
        isinstance(review, dict)
        and set(review)
        == {
            "schema_version",
            "game_id",
            "original_input_sha256",
            "old_date",
            "played_date",
            "scope",
            "authority",
        }
        and review["schema_version"] == VERSION,
        "Date review fields/version differ",
    )
    game = original["game"]
    require(
        review["game_id"] == game["game_id"]
        and review["original_input_sha256"] == original_receipt["sha256"]
        and review["old_date"] == game["game_date"]
        and review["old_date"] != review["played_date"],
        "Date review original binding differs",
    )
    corrected_scope = scope(review["scope"])
    require(
        corrected_scope
        == {
            "league": "nba",
            "season": payload["season"],
            "phase": payload["phase"],
            "date": review["played_date"],
            "home": game["home_team_abbr"],
            "away": game["away_team_abbr"],
        },
        "Date review changes unsupported scope",
    )
    authority = review["authority"]
    require(
        isinstance(authority, dict)
        and set(authority) == {"method", "url", "body", "receipt"}
        and authority["method"] == "operator_reviewed_nba_game_page_web_tool_v1",
        "Unsupported played-date authority",
    )
    authority_receipt = evidence(authority["receipt"])
    require(
        authority_receipt["kind"] in ("reviewed_identity", "synthetic_fixture")
        and authority_receipt["captured_at"] is not None,
        "Played-date authority requires a reviewed observation clock",
    )
    require(
        isinstance(authority["body"], str)
        and 0 < len(authority["body"].encode()) <= 128_000
        and hashlib.sha256(authority["body"].encode()).hexdigest()
        == authority_receipt["sha256"],
        "Authority observation checksum/size differs",
    )
    url = f"https://www.nba.com/game/{corrected_scope['away'].lower()}-vs-{corrected_scope['home'].lower()}-{game['game_id']}"
    require(authority["url"] == url, "Authority URL identity differs")
    day = date.fromisoformat(review["played_date"])
    title = f"{TEAM_NAMES[corrected_scope['away']]} vs {TEAM_NAMES[corrected_scope['home']]} {day.strftime('%b')} {day.day}, {day.year} Game Summary | NBA.com ({url})"
    lines = authority["body"].splitlines()
    require(lines and lines[0] == title, "Authority title/date/matchup differs")
    suffix = (
        "th"
        if 11 <= day.day <= 13
        else {1: "st", 2: "nd", 3: "rd"}.get(day.day % 10, "th")
    )
    displayed = (
        f"{day.strftime('%A')}, {day.strftime('%B')} {day.day}{suffix}, {day.year}"
    )
    dates = [
        re.sub(r"^L\d+: ", "", line)
        for line in lines
        if re.fullmatch(
            r"L\d+: (Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), [A-Z][a-z]+ \d{1,2}(st|nd|rd|th), \d{4}",
            line,
        )
    ]
    require(dates == [displayed], "Authority displayed played date differs/ambiguous")
    require(
        all(
            row["game_date"] == review["played_date"] for row in original["appearances"]
        ),
        "Player dates do not agree with reviewed authority",
    )
    projected = json.loads(canonical(original))
    projected["game"]["game_date"] = review["played_date"]
    result = normalize_archive(projected, registry, receipt)
    result["semantic"]["date_basis"] = VERSION
    result["fact_revision"] = digest(result["semantic"])
    result["date_review"] = {
        "original_receipt": original_receipt,
        "review": review,
        "review_sha256": digest(review),
    }
    result.pop("observation_id")
    result["observation_id"] = digest(result)
    return json.loads(canonical(result))
