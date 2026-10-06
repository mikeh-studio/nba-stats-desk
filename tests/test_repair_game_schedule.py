"""Official schedule reconciliation must preserve neutral games and reject mismatches."""

from copy import deepcopy

import pytest
from app.agent.semantic_source import snapshot_digest
from app.agent.semantics import COMPONENTS
from scripts.repair_game_schedule import reconcile_schedule


def inputs():
    rows = [
        dict(
            dict.fromkeys(COMPONENTS, 0),
            season="2024-25",
            season_type="Regular Season",
            game_id="001",
            game_date="2025-01-01",
            player_id=i + 1,
            player_name=f"Example {i}",
            team_abbr=team,
            opponent_abbr=opponent,
            home_away="AWAY",
            matchup=f"{team} @ {opponent}",
            pts=10,
            min=20,
        )
        for i, (team, opponent) in enumerate([("AAA", "BBB"), ("BBB", "AAA")])
    ]
    snapshot = dict(
        rows=rows,
        coverage=[
            dict(
                season="2024-25",
                phase="Regular Season",
                rows=2,
                data_through="2025-01-01",
            )
        ],
        sources=["synthetic"],
        source_timestamp="2025-01-02T00:00:00Z",
        capture={},
    )
    snapshot["sha256"] = snapshot_digest(snapshot)
    game = dict(
        gameId="001",
        gameDateEst="2025-01-01T00:00:00",
        gameStatus=3,
        isNeutral=True,
        homeTeam=dict(teamTricode="AAA", score=10),
        awayTeam=dict(teamTricode="BBB", score=10),
    )
    return snapshot, dict(
        leagueSchedule=dict(seasonYear="2024-25", gameDates=[dict(games=[game])])
    )


def test_neutral_game_uses_official_designation_and_preserves_raw_matchup():
    snapshot, schedule = inputs()
    repaired, rows, audit = reconcile_schedule(snapshot, schedule)
    assert [r["home_away"] for r in repaired["rows"]] == ["HOME", "AWAY"]
    assert repaired["rows"][0]["source_matchup"] == "AAA @ BBB"
    assert len(rows) == 2 and audit["neutral_team_games"] == 2
    assert len(audit["changed_assignments"]) == 1
    assert snapshot["rows"][0]["home_away"] == "AWAY"


@pytest.mark.parametrize("change", ["score", "date", "missing", "unfinished"])
def test_unverified_schedule_cannot_repair(change):
    snapshot, schedule = inputs()
    schedule = deepcopy(schedule)
    game = schedule["leagueSchedule"]["gameDates"][0]["games"][0]
    if change == "score":
        game["homeTeam"]["score"] = 99
    elif change == "date":
        game["gameDateEst"] = "2025-01-02"
    elif change == "missing":
        schedule["leagueSchedule"]["gameDates"] = []
    else:
        game["gameStatus"] = 2
    with pytest.raises(ValueError):
        reconcile_schedule(snapshot, schedule)
