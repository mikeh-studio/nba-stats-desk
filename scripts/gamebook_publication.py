"""Operational PDF publication expectation, independent of source availability."""

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from scripts.gamebook_store import require

PUBLICATION_LAG_DAYS = 1
LEAGUE_TIMEZONE = "America/New_York"


def due_through(as_of):
    """Next league calendar day, not 24 elapsed hours after tipoff."""
    require(as_of.tzinfo is not None, "Publication clock must be timezone aware")
    return as_of.astimezone(ZoneInfo(LEAGUE_TIMEZONE)).date() - timedelta(
        days=PUBLICATION_LAG_DAYS
    )


def publication_report(games, as_of, id_field):
    cutoff = due_through(as_of)
    waiting = [
        {
            id_field: game[id_field],
            "game_date": game["date"],
            "expected_on": str(
                date.fromisoformat(game["date"]) + timedelta(days=PUBLICATION_LAG_DAYS)
            ),
            "status": "not_yet_due",
        }
        for game in games
        if date.fromisoformat(game["date"]) > cutoff
    ]
    return {
        "pdf_publication_lag_days": PUBLICATION_LAG_DAYS,
        "publication_timezone": LEAGUE_TIMEZONE,
        "pdf_due_through": str(cutoff),
        "not_yet_due": len(waiting),
        "publication_waits": waiting,
    }
