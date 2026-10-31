"""Offline ESPN observations from retained raw summaries and explicit aliases."""

from __future__ import annotations

import hashlib
import re

from scripts import espn_corrections as layers

from .historical import COUNTS, validated_stats
from .identity import canonical, digest, evidence, require, scope


def normalize(payload, registry, receipt):
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
            "source_body",
            "source",
        },
        "ESPN retained game fields differ",
    )
    require(
        payload["schema_version"] == "espn_retained_game/1"
        and payload["provider"] == "espn"
        and payload["season"] == "2026-27"
        and payload["phase"] == "Regular Season",
        "ESPN adapter scope is 2026-27 regular season only",
    )
    receipt = evidence(receipt)
    require(
        receipt["kind"]
        in ("archive_snapshot", "provider_capture", "synthetic_fixture"),
        "Identity-only evidence cannot authorize box observations",
    )
    source = evidence(payload["source"])
    require(
        source["kind"] in ("provider_capture", "synthetic_fixture")
        and source["captured_at"] is not None,
        "ESPN requires original capture lineage",
    )
    require(
        isinstance(payload["source_body"], str),
        "Retain the original source bytes as UTF-8",
    )
    raw = payload["source_body"].encode("utf-8")
    require(
        0 < len(raw) <= 750_000 and hashlib.sha256(raw).hexdigest() == source["sha256"],
        "ESPN original source hash/size differs",
    )
    summary = parse_json(raw)
    game = payload["game"]
    require(
        game["season"] == payload["season"] and game["phase"] == payload["phase"],
        "ESPN declared scope differs",
    )
    game_scope = scope(
        {
            "league": "nba",
            "season": game["season"],
            "phase": game["phase"],
            "date": game["date"],
            "home": game["home"],
            "away": game["away"],
        }
    )
    # Strengthen Python truthiness/coercion at the raw source boundary. Existing
    # result/box validation is reused unchanged for scope, status and arithmetic.
    require(
        type(game["final"]) is bool and game["final"] and game["provider"] == "espn",
        "Invalid ESPN final",
    )
    require(
        all(type(game[side + "_score"]) is int for side in ("home", "away")),
        "ESPN game scores must be integral",
    )
    for team in summary["boxscore"]["players"]:
        for group in team["statistics"]:
            for athlete in group["athletes"]:
                require(
                    type(athlete["didNotPlay"]) is bool,
                    "Explicit boolean didNotPlay required",
                )
                if not athlete["didNotPlay"]:
                    require(
                        isinstance(athlete["stats"], list)
                        and all(isinstance(cell, str) for cell in athlete["stats"]),
                        "ESPN stat cells must retain source text",
                    )
    result = layers.normalize(summary, game, "result")
    box = layers.normalize(summary, game, "box")
    result_revision, box_revision = layers.validate(result), layers.validate(box)
    require(box["result_revision"] == result_revision, "ESPN result/box chain differs")
    uid = registry.register_game("espn", game["source_game_id"], game_scope, source)
    players, participants, bindings, seen = [], [], [], set()
    require(0 < len(box["players"]) <= 100, "ESPN participant bound exceeded")
    for player in box["players"]:
        sid = player["source_player_id"]
        binding = registry.resolve_person("espn", sid)
        require(binding is not None, "Unresolved ESPN player alias: " + sid)
        pid, version = binding
        require(pid not in seen, "Duplicate canonical appearance/participant")
        seen.add(pid)
        participants.append(
            {"player_id": pid, "team": player["team"], "played": player["played"]}
        )
        bindings.append({"source_player_id": sid, "player_id": pid, "version": version})
        if not player["played"]:
            continue
        stats = player["stats"]
        values = {
            key: stats[key.upper() if key != "tov" else "TO"]
            for key in COUNTS
            if key != "plus_minus"
        }
        values["plus_minus"] = stats["PLUS_MINUS"]
        values = validated_stats(values)
        minute = stats["MIN"]
        require(
            isinstance(minute, str)
            and (minute == "--" or re.fullmatch(r"\d{1,2}", minute)),
            "Unsupported ESPN minute precision; explicit policy required",
        )
        minutes = None if minute == "--" else int(minute)
        require(minutes is None or minutes <= 75, "ESPN minutes exceed source bound")
        side = "home" if player["team"] == game["home"] else "away"
        players.append(
            {
                "player_id": pid,
                "team_at_game": game[side],
                "opponent": game["away" if side == "home" else "home"],
                "home_away": side.upper(),
                "played": True,
                "stats": values,
                "minutes": {
                    "value": minutes,
                    "precision": "unknown"
                    if minutes is None
                    else "whole_minute_reported",
                    "zero_origin": "reported_zero" if minutes == 0 else None,
                },
            }
        )
    semantic = {
        "schema_version": "unified_game_observation/1",
        "adapter_version": "espn_retained_observation/1",
        "semantics_version": "unified_semantics/1.0.0",
        "source_definition": "espn_summary_reported_v1",
        "provider": "espn",
        "game_uid": uid,
        "scope": game_scope,
        "scope_version": registry.game_scope(uid)[1],
        "result": {
            "home_score": game["home_score"],
            "away_score": game["away_score"],
            "is_overtime": None,
        },
        "appearances": sorted(players, key=lambda p: p["player_id"]),
        "identity_bindings": sorted(bindings, key=lambda p: p["source_player_id"]),
        "membership_level": "provider_declared_complete",
        "capabilities": {
            "runtime_eligible": False,
            "league_ranks": False,
            "exact_seconds": False,
            "pdf_corroborated": False,
        },
    }
    normalized = {
        "semantic": semantic,
        "fact_revision": digest(semantic),
        "source_game_id": game["source_game_id"],
        "provenance": receipt,
        "source_chain": {
            "source": source,
            "result_revision": result_revision,
            "box_revision": box_revision,
            "definition": layers.VERSION,
        },
        "membership": {
            "level": "provider_declared_complete",
            "participant_manifest": {
                "sha256": digest(participants),
                "evidence": source,
                "participants": sorted(participants, key=lambda p: p["player_id"]),
            },
        },
    }
    normalized["observation_id"] = digest(normalized)
    import json

    return json.loads(canonical(normalized))
