"""The production season, read from the repository's single season setting."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml

SEASON_FILE = Path(__file__).resolve().parents[1] / "season.yml"
FIRST_ARCHIVE_SEASON = "2023-24"


def _load_current_season(path: Path = SEASON_FILE) -> str:
    with path.open("r", encoding="utf-8") as handle:
        season = str((yaml.safe_load(handle) or {}).get("current_season", ""))
    if not re.fullmatch(r"\d{4}-\d{2}", season) or (int(season[:4]) + 1) % 100 != int(
        season[5:]
    ):
        raise ValueError(f"{path} must set current_season like '2026-27'")
    return season


CURRENT_SEASON = _load_current_season()


def season_label(start_year: int) -> str:
    return f"{start_year}-{(start_year + 1) % 100:02d}"


# Archives are isolated datasets; the current season keeps unsuffixed names.
SUPPORTED_SEASONS = tuple(
    season_label(year)
    for year in range(int(CURRENT_SEASON[:4]), int(FIRST_ARCHIVE_SEASON[:4]) - 1, -1)
)
ARCHIVE_SEASONS = SUPPORTED_SEASONS[1:]


def season_date_bounds(season: str) -> tuple[date, date]:
    """Inclusive July 1 through June 30 window for a supported season."""
    if season not in SUPPORTED_SEASONS:
        raise ValueError(f"Unsupported season: {season}")
    year = int(season[:4])
    return date(year, 7, 1), date(year + 1, 6, 30)
