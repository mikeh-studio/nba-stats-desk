"""Pure retained ESPN payload projection; shared by offline and legacy validators."""

import copy
from typing import Any

from scripts.gamebook_store import require


def source_input(payload):
    """Keep only fields needed for reproducible event and box reconciliation."""
    header = payload["header"]
    competition = header["competitions"]
    require(len(competition) == 1, "Ambiguous competition")
    comp = competition[0]
    result: dict[str, Any] = {
        "header": {
            "id": header["id"],
            "season": header["season"],
            "competitions": [
                {
                    "id": comp["id"],
                    "date": comp["date"],
                    "status": {"type": comp["status"]["type"]},
                    "competitors": [
                        {
                            "homeAway": t["homeAway"],
                            "score": t["score"],
                            "team": {k: t["team"][k] for k in ("id", "abbreviation")},
                        }
                        for t in comp["competitors"]
                    ],
                }
            ],
        },
        "boxscore": {"players": []},
        "plays": [],
    }
    for team in payload["boxscore"]["players"]:
        groups = []
        for group in team["statistics"]:
            groups.append(
                {
                    "labels": group["labels"],
                    "athletes": [
                        {
                            "athlete": {
                                k: a["athlete"][k] for k in ("id", "displayName")
                            },
                            "didNotPlay": a["didNotPlay"],
                            "stats": a.get("stats", []),
                        }
                        for a in group["athletes"]
                    ],
                }
            )
        result["boxscore"]["players"].append(
            {
                "team": {k: team["team"][k] for k in ("id", "abbreviation")},
                "statistics": groups,
            }
        )
    for play in payload["plays"]:
        row = {
            k: play[k]
            for k in (
                "id",
                "text",
                "scoringPlay",
                "scoreValue",
                "homeScore",
                "awayScore",
            )
        }
        row.update(
            period={"number": play["period"]["number"]},
            clock={"displayValue": play["clock"]["displayValue"]},
            team={"id": play.get("team", {}).get("id")},
            participants=[
                {"athlete": {"id": pid}}
                for pid in sorted(
                    {p["athlete"]["id"] for p in play.get("participants", [])}
                )
            ],
        )
        result["plays"].append(row)
    return copy.deepcopy(result)
