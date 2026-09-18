#!/usr/bin/env python3
"""Recover a local season snapshot from frozen official player AND team logs.

No warehouse writes or model calls. Network reads require --capture. Raw responses
and prior snapshots are preserved. Validation fails before a snapshot is emitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.agent.semantic_source import (  # noqa: E402
    load_snapshot,
    snapshot_digest,
    snapshot_evidence,
)

PHASES = ("Regular Season", "Playoffs")
COUNTS = "PTS REB AST STL BLK TOV FGM FGA FG3M FG3A FTM FTA OREB DREB PF".split()
RECONCILE = [c for c in COUNTS if c not in ("TOV", "PF")]
SOURCE = "https://stats.nba.com/stats/leaguegamelog"


def capture_sources(directory, season="2025-26"):
    """Four bounded public-source reads; preserve previously captured responses."""
    from nba_api.stats.endpoints import leaguegamelog

    directory.mkdir(parents=True, exist_ok=True)
    for phase in PHASES:
        for entity in ("P", "T"):
            path = directory / f"{phase.replace(' ', '_')}_{entity}.json"
            if path.exists():
                raw_rows(path, season, phase, entity)
                continue
            api = leaguegamelog.LeagueGameLog(
                player_or_team_abbreviation=entity,
                season=season,
                season_type_all_star=phase,
                timeout=25,
            )
            document = {
                "source": SOURCE,
                "parameters": api.parameters,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "payload": api.get_dict(),
            }
            with path.open("x") as handle:
                json.dump(document, handle)


def raw_rows(path, season, phase, entity):
    document = json.loads(path.read_text())
    params = document["parameters"]
    if document["source"] != SOURCE or any(
        params.get(k) != v
        for k, v in {
            "Season": season,
            "SeasonType": phase,
            "PlayerOrTeam": entity,
        }.items()
    ):
        raise ValueError(f"Source scope mismatch: {path}")
    result = document["payload"]["resultSets"][0]
    if any(len(r) != len(result["headers"]) for r in result["rowSet"]):
        raise ValueError("Malformed source row")
    return [dict(zip(result["headers"], r, strict=True)) for r in result["rowSet"]]


def validate_phase(players, teams, season, phase, *, complete_season=True):
    """Reconcile actual appearances; team turnovers include non-player turnovers."""
    if not players or not teams:
        raise ValueError("Empty source")
    prefix = ("002" if phase == "Regular Season" else "004") + season[2:4]
    expected_season_id = ("2" if phase == "Regular Season" else "4") + season[:4]
    for row in players + teams:
        if (
            not row["GAME_ID"].startswith(prefix)
            or str(row["SEASON_ID"]) != expected_season_id
        ):
            raise ValueError("Wrong season/phase")
        if not f"{season[:4]}-07-01" <= row["GAME_DATE"] <= f"20{season[-2:]}-06-30":
            raise ValueError("Date outside season")
        for column in [*COUNTS, "MIN"]:
            v = row.get(column)
            if not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise ValueError(f"Missing/invalid {column}")
            if column != "MIN" and int(v) != v:
                raise ValueError(f"Non-integer {column}")
        if any(
            row[made] > row[attempted]
            for made, attempted in [("FGM", "FGA"), ("FG3M", "FG3A"), ("FTM", "FTA")]
        ):
            raise ValueError("Makes exceed attempts")
        if row["FG3M"] > row["FGM"] or row["FG3A"] > row["FGA"]:
            raise ValueError("Invalid three-point components")
        if (
            row["PTS"] != 2 * row["FGM"] + row["FG3M"] + row["FTM"]
            or row["REB"] != row["OREB"] + row["DREB"]
        ):
            raise ValueError("Inconsistent scoring/rebounding identity")
    if len({(r["GAME_ID"], r["PLAYER_ID"]) for r in players}) != len(players):
        raise ValueError("Duplicate player key")
    team_lookup = {(r["GAME_ID"], r["TEAM_ABBREVIATION"]): r for r in teams}
    if len(team_lookup) != len(teams):
        raise ValueError("Duplicate team key")
    games = defaultdict(list)
    for r in teams:
        games[r["GAME_ID"]].append(r)
    for pair in games.values():
        if len(pair) != 2 or {r["WL"] for r in pair} != {"W", "L"}:
            raise ValueError("Expected two teams and one winner")
        for own, opp in (pair, pair[::-1]):
            if (
                own["GAME_DATE"] != opp["GAME_DATE"]
                or own["MATCHUP"].split()[-1] != opp["TEAM_ABBREVIATION"]
                or (own["PTS"] > opp["PTS"]) != (own["WL"] == "W")
            ):
                raise ValueError("Conflicting team/game context")
    if complete_season:
        if phase == "Regular Season":
            counts = Counter(r["TEAM_ABBREVIATION"] for r in teams)
            if len(games) != 1230 or len(counts) != 30 or set(counts.values()) != {82}:
                raise ValueError("Incomplete regular season")
        else:
            wins = defaultdict(Counter)
            for r in teams:
                if r["WL"] == "W":
                    wins[r["GAME_ID"][:-1]][r["TEAM_ABBREVIATION"]] += 1
            if len(wins) != 15 or any(max(c.values()) != 4 for c in wins.values()):
                raise ValueError("Incomplete playoffs")
    grouped = defaultdict(list)
    for r in players:
        grouped[r["GAME_ID"], r["TEAM_ABBREVIATION"]].append(r)
    if grouped.keys() != team_lookup.keys():
        raise ValueError("Player/team game coverage mismatch")
    for key, group in grouped.items():
        team = team_lookup[key]
        if any(
            r["GAME_DATE"] != team["GAME_DATE"]
            or r["MATCHUP"] != team["MATCHUP"]
            or r["WL"] != team["WL"]
            for r in group
        ):
            raise ValueError("Player/team context mismatch")
        for column in RECONCILE:
            if sum(r[column] for r in group) != team[column]:
                raise ValueError(f"Player/team {column} mismatch: {key}")
        # League game logs round each player's minutes to whole minutes.
        if abs(sum(r["MIN"] for r in group) - team["MIN"]) > len(group) * 0.5:
            raise ValueError("Player/team minutes exceed rounding bound")
    return {
        "player_rows": len(players),
        "team_games": len(teams),
        "games": len(games),
        "reconciled_columns": RECONCILE,
        "status": "passed",
    }


def repair(directory, original_path, season="2025-26"):
    original, _ = load_snapshot(original_path)
    rows, phases, sources = [], {}, []
    for phase in PHASES:
        tag = phase.replace(" ", "_")
        paths = [directory / f"{tag}_{entity}.json" for entity in ("P", "T")]
        players, teams = [
            raw_rows(p, season, phase, e)
            for p, e in zip(paths, ("P", "T"), strict=True)
        ]
        phases[phase] = validate_phase(players, teams, season, phase)
        sources.extend(
            {
                "path": str(p.resolve()),
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            }
            for p in paths
        )
        for r in players:
            rows.append(
                {
                    "season": season,
                    "season_type": phase,
                    "game_id": r["GAME_ID"],
                    "player_id": r["PLAYER_ID"],
                    "player_name": r["PLAYER_NAME"],
                    "game_date": r["GAME_DATE"],
                    "team_abbr": r["TEAM_ABBREVIATION"],
                    "opponent_abbr": r["MATCHUP"].split()[-1],
                    **{c.lower(): r[c] for c in [*COUNTS, "MIN", "PLUS_MINUS"]},
                }
            )
    rows.sort(key=lambda r: (r["season"], r["game_date"], r["game_id"], r["player_id"]))

    def key(r):
        return (
            r["season"],
            r["season_type"],
            r["game_date"],
            r["team_abbr"],
            r["player_id"],
        )

    old, new = ({key(r): r for r in sample} for sample in (original["rows"], rows))
    if (
        len(old) != len(original["rows"])
        or len(new) != len(rows)
        or old.keys() - new.keys()
    ):
        raise ValueError(
            "Duplicate natural key or original appearance missing from repair"
        )
    changes = [
        {"key": list(k), "column": c, "before": old[k][c], "after": new[k][c]}
        for k in sorted(old)
        for c in [
            "pts",
            "reb",
            "ast",
            "stl",
            "blk",
            "tov",
            "min",
            "fgm",
            "fga",
            "fg3m",
            "fg3a",
            "ftm",
            "fta",
            "plus_minus",
        ]
        if old[k].get(c) is not None and old[k][c] != new[k][c]
    ]
    audit = {
        "status": "passed",
        "original_sha256": original["sha256"],
        "phases": phases,
        "raw_sources": sources,
        "before_rows": len(old),
        "after_rows": len(new),
        "added_appearances": [list(k) for k in sorted(new.keys() - old.keys())],
        "changed_existing_values": changes,
        "canonicalized_game_ids": sum(
            old[k]["game_id"] != new[k]["game_id"] for k in old
        ),
        "missing_before": {
            c: sum(r.get(c.lower()) is None for r in original["rows"]) for c in COUNTS
        },
        "missing_after": {
            c: sum(r.get(c.lower()) is None for r in rows) for c in COUNTS
        },
        "model_tokens": 0,
        "warehouse_mutations": 0,
    }
    snapshot = {
        "version": 1,
        "evidence_kind": "historical",
        "sources": [SOURCE],
        "source_timestamp": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
        "coverage": [
            {
                "season": season,
                "phase": phase,
                "rows": sum(r["season_type"] == phase for r in rows),
                "data_through": max(
                    r["game_date"] for r in rows if r["season_type"] == phase
                ),
            }
            for phase in PHASES
        ],
        "capture": {"raw_sources": sources, "repair_of": original["sha256"]},
        "limitations": [
            "Retrospective official statistics, not contemporaneous warehouse knowledge.",
            "Player minutes are rounded individually by the league game-log source.",
            "Team totals validate points, shooting and rebounding; team turnovers may include non-player turnovers.",
            "No new roster, injury-report or exact tipoff coverage is implied.",
        ],
    }
    snapshot["sha256"] = snapshot_digest(snapshot)
    snapshot_evidence(snapshot)
    audit["repaired_sha256"] = snapshot["sha256"]
    return snapshot, audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--capture",
        action="store_true",
        help="Retrieve four public NBA source responses before local validation",
    )
    args = parser.parse_args()
    if args.capture:
        capture_sources(args.source_dir)
    snapshot, audit = repair(args.source_dir, args.original)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    for name, value in [("snapshot", snapshot), ("repair-audit", audit)]:
        (args.output_dir / f"{name}.json").write_text(
            json.dumps(value, indent=2) + "\n"
        )
    print(
        json.dumps(
            {
                k: v
                for k, v in audit.items()
                if k
                not in ("added_appearances", "raw_sources", "changed_existing_values")
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
