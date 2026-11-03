"""Synthetic espn factories; source and policy profiles remain explicit."""

from __future__ import annotations

from datetime import date

from scripts import espn_sources as src

SEASON = "2025-26"


DAY = date(2025, 10, 22)


REF = [
    {"id": 10, "full_name": "Avery Able"},
    {"id": 11, "full_name": "Blake Baker"},
    {"id": 20, "full_name": "Casey Cole"},
]


def fixtures():
    status = {"completed": True, "state": "post", "name": "STATUS_FINAL"}
    teams = [
        {"homeAway": "home", "team": {"abbreviation": "NY", "id": "18"}, "score": "3"},
        {"homeAway": "away", "team": {"abbreviation": "BOS", "id": "2"}, "score": "0"},
    ]
    event = {
        "id": "1234567",
        "season": {"year": 2026, "type": 2},
        "competitions": [
            {
                "id": "1234567",
                "date": "2025-10-22T23:00Z",
                "status": {"type": status},
                "competitors": teams,
            }
        ],
    }
    labels = [
        "MIN",
        "PTS",
        "FG",
        "3PT",
        "FT",
        "REB",
        "AST",
        "TO",
        "STL",
        "BLK",
        "OREB",
        "DREB",
        "PF",
        "+/-",
    ]

    def athlete(pid, name, points, assist):
        return {
            "athlete": {"id": pid, "displayName": name},
            "didNotPlay": False,
            "stats": [
                "24",
                str(points),
                "1-1" if points else "0-0",
                "1-1" if points else "0-0",
                "0-0",
                "0",
                str(assist),
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
            ],
        }

    players = [
        {"team": t["team"], "statistics": [{"labels": labels, "athletes": athletes}]}
        for t, athletes in zip(
            teams,
            [
                [
                    athlete("100", "Avery Able", 0, 1),
                    athlete("101", "Blake Baker", 3, 0),
                ],
                [athlete("200", "Casey Cole", 0, 0)],
            ],
        )
    ]
    play = {
        "id": "90001",
        "text": "Blake Baker makes 25-foot three point jumper (Avery Able assists)",
        "period": {"number": 1},
        "clock": {"displayValue": "11:00"},
        "scoringPlay": True,
        "scoreValue": 3,
        "team": {"id": "18"},
        "participants": [{"athlete": {"id": "100"}}, {"athlete": {"id": "101"}}],
        "homeScore": 3,
        "awayScore": 0,
    }
    plays = [play] + [
        {
            **play,
            "id": str(90000 + i),
            "period": {"number": i},
            "clock": {"displayValue": "0.0"},
            "text": "End of Game" if i == 4 else "End of Period",
            "scoringPlay": False,
            "scoreValue": 0,
        }
        for i in (2, 3, 4)
    ]
    schedule = {"leagues": [{"season": {"displayName": SEASON}}], "events": [event]}
    summary = {"header": event, "boxscore": {"players": players}, "plays": plays}
    return schedule, summary


def candidate():
    schedule, summary = fixtures()
    game = src.scoreboard(schedule, SEASON, DAY)[0]
    result = src.normalize_summary(summary, game, REF)
    result.update(
        nba_game_id="0022500001",
        pdf_validation={
            "status": "matched",
            "document_sha256": "synthetic",
            "matches": [
                {
                    "source_event_id": "90001",
                    "pdf_event_id": "synthetic-event",
                    "page": 2,
                    "url": "https://example.test/book.pdf#page=2",
                }
            ],
        },
    )
    return result
