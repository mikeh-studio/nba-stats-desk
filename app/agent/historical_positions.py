"""Offline normalization of retained, season-specific NBA roster evidence.

No network or warehouse access here. Operators retain and review source bytes;
checksums establish replay integrity, not publisher authenticity.
"""

import hashlib
import json
import re
from datetime import datetime

MAPPING_VERSION = "position_groups/2"
GROUPS = {
    "G": ("Guard",),
    "PG": ("Guard",),
    "SG": ("Guard",),
    "F": ("Wing",),
    "SF": ("Wing",),
    "PF": ("Wing",),
    "C": ("Big",),
    "G-F": ("Guard", "Wing"),
    "F-G": ("Guard", "Wing"),
    "F-C": ("Wing", "Big"),
    "C-F": ("Wing", "Big"),
}
LABELS = {"Guard": "guards", "Wing": "wings/forwards", "Big": "bigs/centers"}


def digest(context):
    payload = {k: v for k, v in context.items() if k != "sha256"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def season_valid(season):
    return (
        isinstance(season, str)
        and bool(re.fullmatch(r"20\d{2}-\d{2}", season))
        and int(season[-2:]) == (int(season[:4]) + 1) % 100
    )


def roster_records(raw, season, source_ref, source_sha256):
    """Read archived CommonTeamRoster responses with exact NBA identifiers."""
    if not season_valid(season) or len(raw.get("rosters", {})) != 30:
        raise ValueError("Expected one complete 30-team season capture")
    datetime.fromisoformat(raw["fetched"].replace("Z", "+00:00"))
    records = {}
    for team_id, response in raw["rosters"].items():
        params = response["parameters"]
        if (
            response["resource"] != "commonteamroster"
            or params["Season"] != season
            or str(params["TeamID"]) != team_id
            or params["LeagueID"] != "00"
        ):
            raise ValueError("Roster request identity or season mismatch")
        tables = [t for t in response["resultSets"] if t["name"] == "CommonTeamRoster"]
        if len(tables) != 1:
            raise ValueError("Missing or duplicate roster result set")
        table = tables[0]
        for values in table["rowSet"]:
            if len(values) != len(table["headers"]):
                raise ValueError("Malformed roster row")
            row = dict(zip(table["headers"], values, strict=True))
            pid, position = row["PLAYER_ID"], row["POSITION"]
            if (
                str(row["TeamID"]) != team_id
                or str(row["SEASON"]) != season[:4]
                or row["LeagueID"] != "00"
                or type(pid) is not int
                or pid <= 0
                or position not in GROUPS
            ):
                raise ValueError("Invalid historical roster row")
            if pid in records:
                # Even agreeing duplicate rows need an explicit operator resolution.
                raise ValueError("Duplicate season/player roster identity")
            records[pid] = dict(
                season=season,
                player_id=pid,
                position=position,
                provenance="season_roster",
                source_ref=source_ref,
                source_sha256=source_sha256,
                source_locator=f"TeamID={team_id};PLAYER_ID={pid}",
                source_date=raw["fetched"][:10],
            )
    return list(records.values())


def make_context(records, seasons):
    context = dict(
        mapping_version=MAPPING_VERSION,
        approved_seasons=sorted(seasons),
        records=records,
    )
    context["sha256"] = digest(context)
    return context
