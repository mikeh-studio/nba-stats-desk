"""Synthetic historical factories; source and policy profiles remain explicit."""

from __future__ import annotations

import copy
import hashlib

from app.data.unified.identity import canonical, digest


def receipt(body=b"synthetic evidence", captured_at=None, kind="synthetic_fixture"):
    return {
        "sha256": hashlib.sha256(body).hexdigest(),
        "reference": "synthetic:historical-fixture",
        "captured_at": captured_at,
        "kind": kind,
    }


def payload(gid="0022500001", day="2025-10-22"):
    def player(pid, team, points):
        home = team == "NYK"
        return {
            "game_id": gid,
            "player_id": pid,
            "player_name": "Shared Synthetic Name",
            "team_abbr": team,
            "opponent_abbr": "BOS" if home else "NYK",
            "home_away": "HOME" if home else "AWAY",
            "game_date": day,
            "season": "2025-26",
            "season_type": "Regular Season",
            "min": 0 if pid == 12 else 20,
            "pts": points,
            "fgm": points // 2,
            "fga": points // 2,
            "fg3m": 0,
            "fg3a": 0,
            "ftm": 0,
            "fta": 0,
            "oreb": 0,
            "dreb": 1,
            "reb": 1,
            "ast": None if pid == 12 else 0,
            "stl": 0,
            "blk": 0,
            "tov": 0,
            "pf": 0,
            "plus_minus": None,
        }

    return {
        "schema_version": "nba_archive_game/1",
        "provider": "nba_api",
        "season": "2025-26",
        "phase": "Regular Season",
        "game": {
            "game_id": gid,
            "game_date": day,
            "season": "2025-26",
            "home_team_abbr": "NYK",
            "away_team_abbr": "BOS",
            "home_team_pts": 4,
            "away_team_pts": 2,
            "is_overtime": None,
        },
        "appearances": [
            player(10, "NYK", 4),
            player(11, "BOS", 2),
            player(12, "BOS", 0),
        ],
    }


def complete(value):
    value = copy.deepcopy(value)
    value.pop("membership", None)
    game = value["game"]
    value["membership"] = {
        "schema_version": "nba_participants/1",
        "game_id": game["game_id"],
        "scope": {
            "league": "nba",
            "season": "2025-26",
            "phase": "Regular Season",
            "date": game["game_date"],
            "home": "NYK",
            "away": "BOS",
        },
        "input_game_hash": digest(value),
        "participants": [
            {"player_id": p["player_id"], "team": p["team_abbr"], "played": True}
            for p in value["appearances"]
        ]
        + [{"player_id": 99, "team": "NYK", "played": False}],
        "evidence": receipt(captured_at="2025-10-23T12:00:00+00:00"),
    }
    return value


def ingest(store, value, *, expected=None, captured_at=None):
    body = canonical(value).encode()
    return store.ingest_bytes(
        body, receipt(body, captured_at), expected_revision=expected
    )
