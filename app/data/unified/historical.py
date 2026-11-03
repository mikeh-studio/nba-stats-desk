"""Normalize explicitly scoped, retained 2025-26 NBA archive game bundles."""

from __future__ import annotations

import math
from typing import Any

from .identity import canonical, digest, evidence, person_id, require, scope

ADAPTER = "nba_archive_observation/1"
COUNTS = (
    "pts",
    "fgm",
    "fga",
    "fg3m",
    "fg3a",
    "ftm",
    "fta",
    "oreb",
    "dreb",
    "reb",
    "ast",
    "stl",
    "blk",
    "tov",
    "pf",
    "plus_minus",
)


def count(value, name, nullable=True):
    if value is None and nullable:
        return None
    require(
        type(value) in (int, float)
        and -(2**53) < value < 2**53
        and math.isfinite(value)
        and int(value) == value,
        "Invalid integral " + name,
    )
    require(-(2**53) < value < 2**53, "Count exceeds exact integer range")
    require(value >= 0 or name == "plus_minus", "Negative " + name)
    return int(value)


def validated_stats(row):
    stats = {key: count(row.get(key), key) for key in COUNTS}
    require(
        all(stats[key] is not None for key in ("pts", "fgm", "fg3m", "ftm")),
        "Missing required scoring components",
    )
    require(
        stats["pts"] == 2 * stats["fgm"] + stats["fg3m"] + stats["ftm"],
        "Player scoring arithmetic differs",
    )
    require(stats["fg3m"] <= stats["fgm"], "Three-point makes exceed field goals")
    require(stats["pts"] <= 100, "Points exceed source contract")
    for key in COUNTS:
        if key not in ("pts", "plus_minus") and stats[key] is not None:
            require(stats[key] <= 75, "Count exceeds source contract")
    for made, attempt in (("fgm", "fga"), ("fg3m", "fg3a"), ("ftm", "fta")):
        if stats[attempt] is not None:
            require(stats[made] <= stats[attempt], "Makes exceed attempts")
    if stats["fga"] is not None and stats["fg3a"] is not None:
        require(
            stats["fg3a"] <= stats["fga"]
            and stats["fgm"] - stats["fg3m"] <= stats["fga"] - stats["fg3a"],
            "Invalid two/three-point attempt split",
        )
    if all(stats[k] is not None for k in ("reb", "oreb", "dreb")):
        require(
            stats["reb"] == stats["oreb"] + stats["dreb"],
            "Rebound arithmetic differs",
        )
    return stats


def normalize(payload, registry, receipt):
    if (
        isinstance(payload, dict)
        and payload.get("schema_version") == "nba_archive_date_review/1"
    ):
        from .date_review import normalize as normalize_review

        return normalize_review(payload, registry, receipt)
    require(
        isinstance(payload, dict)
        and set(payload)
        in (
            {"schema_version", "provider", "season", "phase", "game", "appearances"},
            {
                "schema_version",
                "provider",
                "season",
                "phase",
                "game",
                "appearances",
                "membership",
            },
        ),
        "Archive bundle fields differ",
    )
    require(
        payload["schema_version"] == "nba_archive_game/1"
        and payload["provider"] == "nba_api",
        "Explicit NBA archive source required",
    )
    require(
        payload["season"] == "2025-26" and payload["phase"] == "Regular Season",
        "Historical adapter scope is 2025-26 regular season only",
    )
    receipt = evidence(receipt)
    require(
        receipt["kind"]
        in ("archive_snapshot", "provider_capture", "synthetic_fixture"),
        "Identity-only evidence cannot authorize box observations",
    )
    game = payload["game"]
    require(game["season"] == payload["season"], "Game season differs")
    game_scope = scope(
        {
            "league": "nba",
            "season": payload["season"],
            "phase": payload["phase"],
            "date": game["game_date"],
            "home": game["home_team_abbr"],
            "away": game["away_team_abbr"],
        }
    )
    gid = game["game_id"]
    uid = registry.register_game("nba_api", gid, game_scope, receipt)
    scope_version = registry.game_scope(uid)[1]
    scores = {
        side: count(game[side + "_team_pts"], "pts", False) for side in ("home", "away")
    }
    require(scores["home"] != scores["away"], "Tied historical final score")
    overtime = game.get("is_overtime")
    require(overtime is None or type(overtime) is bool, "Invalid overtime flag")
    rows = payload["appearances"]
    require(
        isinstance(rows, list) and 0 < len(rows) <= 100, "Invalid game appearance bound"
    )
    players = []
    seen = set()
    for row in rows:
        pid = person_id(row["player_id"])
        require(pid not in seen, "Duplicate canonical appearance")
        seen.add(pid)
        require(
            row["game_id"] == gid
            and row["game_date"] == game_scope["date"]
            and row["season"] == payload["season"]
            and row["season_type"] == payload["phase"],
            "Appearance scope differs",
        )
        team = row["team_abbr"]
        require(
            team in (game_scope["home"], game_scope["away"]),
            "Appearance team outside game",
        )
        side = "home" if team == game_scope["home"] else "away"
        other = "away" if side == "home" else "home"
        require(
            row["opponent_abbr"] == game_scope[other]
            and row["home_away"] == side.upper(),
            "Opponent/home-away role differs",
        )
        stats = validated_stats(row)
        minutes = row.get("min")
        if minutes is not None:
            require(
                type(minutes) in (int, float)
                and 0 <= minutes <= 75
                and math.isfinite(minutes)
                and int(minutes) == minutes,
                "Unsupported archive minute value/precision",
            )
        registry.attach_person_alias("nba_api", str(pid), pid, receipt)
        players.append(
            {
                "player_id": pid,
                "team_at_game": team,
                "opponent": row["opponent_abbr"],
                "home_away": row["home_away"],
                "played": True,
                "stats": stats,
                "minutes": {
                    "value": float(minutes) if minutes is not None else None,
                    "precision": "whole_minute_reported"
                    if minutes is not None
                    else "unknown",
                    "zero_origin": "unverified_legacy_zero" if minutes == 0 else None,
                },
            }
        )
    for side in ("home", "away"):
        team_players = [p for p in players if p["team_at_game"] == game_scope[side]]
        require(
            team_players
            and sum(p["stats"]["pts"] for p in team_players) == scores[side],
            "Game team scoring totals differ",
        )
    membership: dict[str, Any] = {
        "level": "observed_sample",
        "participant_manifest": None,
    }
    manifest = payload.get("membership")
    if manifest is not None:
        require(
            set(manifest)
            == {
                "schema_version",
                "game_id",
                "scope",
                "input_game_hash",
                "participants",
                "evidence",
            },
            "Membership manifest fields differ",
        )
        require(
            manifest["schema_version"] == "nba_participants/1"
            and manifest["game_id"] == gid
            and manifest["scope"] == game_scope,
            "Participant manifest scope differs",
        )
        bound = {k: v for k, v in payload.items() if k != "membership"}
        require(
            manifest["input_game_hash"] == digest(bound),
            "Participant input binding differs",
        )
        member_evidence = evidence(manifest["evidence"])
        require(
            member_evidence["kind"] in ("provider_capture", "synthetic_fixture")
            and member_evidence["captured_at"] is not None,
            "Participant capture lineage is not established",
        )
        declared = manifest["participants"]
        require(
            isinstance(declared, list) and 0 < len(declared) <= 100,
            "Invalid participant manifest bound",
        )
        expected = set()
        all_ids = set()
        for member in declared:
            require(
                set(member) == {"player_id", "team", "played"},
                "Participant fields differ",
            )
            pid = person_id(member["player_id"])
            require(
                pid not in all_ids
                and member["team"] in (game_scope["home"], game_scope["away"])
                and type(member["played"]) is bool,
                "Invalid/duplicate participant",
            )
            all_ids.add(pid)
            if member["played"]:
                expected.add((pid, member["team"]))
        require(
            expected == {(p["player_id"], p["team_at_game"]) for p in players},
            "Played participant membership differs; score totals are insufficient",
        )
        membership = {
            "level": "provider_declared_complete",
            "participant_manifest": {
                "sha256": digest(manifest),
                "evidence": member_evidence,
                "participants": sorted(declared, key=lambda p: p["player_id"]),
            },
        }
    semantic = {
        "schema_version": "unified_game_observation/1",
        "adapter_version": ADAPTER,
        "semantics_version": "unified_semantics/1.0.0",
        "source_definition": "nba_game_logs_reported_v1",
        "provider": "nba_api",
        "game_uid": uid,
        "scope": game_scope,
        "scope_version": scope_version,
        "result": {
            "home_score": scores["home"],
            "away_score": scores["away"],
            "is_overtime": overtime,
        },
        "appearances": sorted(players, key=lambda p: p["player_id"]),
        "membership_level": membership["level"],
        "capabilities": {
            "runtime_eligible": False,
            "league_ranks": False,
            "exact_seconds": False,
            "pdf_corroborated": False,
        },
    }
    # Source times and receipt bytes identify observations, not statistical content.
    normalized = {
        "semantic": semantic,
        "fact_revision": digest(semantic),
        "source_game_id": gid,
        "provenance": receipt,
        "membership": membership,
    }
    normalized["observation_id"] = digest(normalized)
    # Round-trip breaks every reference to caller-owned mutable input.
    import json

    return json.loads(canonical(normalized))
