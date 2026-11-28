"""Validated season selection and isolated historical warehouse routing."""

import re
from contextvars import ContextVar
from dataclasses import replace
from datetime import date
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from app.config import Settings

SEASON_FILE = Path(__file__).resolve().parents[1] / "season.yml"
FIRST_ARCHIVE_SEASON = "2023-24"
SEASON_TOKEN_RE = re.compile(r"\b(20\d{2})[-–/](20\d{2}|\d{2})\b(?![-/–]\d)")


def _season_label(start_year: int) -> str:
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def normalize_season(start_year: str, end_year: str) -> str:
    """Normalize 2- or 4-digit ending year into a validated canonical season."""
    start = int(start_year)
    expected = (start + 1) % 100
    if len(end_year) == 4 and int(end_year) != start + 1:
        raise ValueError(f"Unsupported season: {start_year}-{end_year}")
    if len(end_year) == 2 and int(end_year) != expected:
        raise ValueError(f"Unsupported season: {start_year}-{end_year}")
    return validate_season(f"{start_year}-{expected:02d}")


def extract_seasons(text: str) -> list[str]:
    """Extract distinct canonical seasons from 2- or 4-digit season mentions."""
    seasons: list[str] = []
    for match in SEASON_TOKEN_RE.finditer(text):
        season = normalize_season(*match.groups())
        if season not in seasons:
            seasons.append(season)
    return seasons


def _load_default_season(path: Path = SEASON_FILE) -> str:
    """Read the one production season setting shared with the pipeline."""
    with path.open("r", encoding="utf-8") as handle:
        season = str((yaml.safe_load(handle) or {}).get("current_season", ""))
    if not re.fullmatch(r"\d{4}-\d{2}", season) or season != _season_label(
        int(season[:4])
    ):
        raise ValueError(f"{path} must set current_season like '2026-27'")
    return season


DEFAULT_SEASON = _load_default_season()
# Newest first. Every season except DEFAULT_SEASON reads its isolated archive
# datasets (for example nba_gold_2025_26); the current season keeps base names.
SEASONS = tuple(
    _season_label(year)
    for year in range(int(DEFAULT_SEASON[:4]), int(FIRST_ARCHIVE_SEASON[:4]) - 1, -1)
)
ARCHIVE_SEASONS = SEASONS[1:]
_selected_season: ContextVar[str] = ContextVar("nba_season", default=DEFAULT_SEASON)


def validate_season(season: str) -> str:
    if season not in SEASONS:
        raise ValueError(f"Unsupported season: {season}")
    return season


def season_bounds(season: str) -> tuple[date, date]:
    year = int(validate_season(season)[:4])
    return date(year, 7, 1), date(year + 1, 6, 30)


@cache
def _playoff_starts(path: Path) -> dict[str, date]:
    with path.open(encoding="utf-8") as handle:
        starts = (yaml.safe_load(handle) or {}).get("playoff_starts")
    if starts is None:
        return {}
    if not isinstance(starts, dict):
        raise ValueError("playoff_starts must map seasons to first-round dates")
    openers = {}
    for season, raw in starts.items():
        start, end = season_bounds(season)
        try:
            opener = date.fromisoformat(str(raw))
        except ValueError as exc:
            raise ValueError(f"Invalid playoff opener for {season}: {raw}") from exc
        if not start <= opener <= end:
            raise ValueError(f"Playoff opener {opener} is outside {season}")
        openers[season] = opener
    return openers


def coverage_bounds(season: str, phase: str) -> tuple[date, date]:
    """Retain the full archive coverage requirement until a playoff opener is known."""
    start, end = season_bounds(season)
    if phase == "Playoffs":
        start = _playoff_starts(SEASON_FILE).get(season, start)
    return start, end


def current_season() -> str:
    return _selected_season.get()


def most_recent_season(as_of: date) -> str:
    """Latest NBA season that has begun, including throughout the offseason."""
    with SEASON_FILE.open("r", encoding="utf-8") as handle:
        starts = yaml.safe_load(handle)["regular_season_starts"]
    eligible = [
        (date.fromisoformat(opening), validate_season(season))
        for season, opening in starts.items()
        if date.fromisoformat(opening) <= as_of
    ]
    if not eligible:
        raise ValueError(
            "The requested date precedes the supported NBA season calendar."
        )
    return max(eligible)[1]


def dataset_for_season(dataset: str, season: str) -> str:
    validate_season(season)
    return (
        dataset if season == DEFAULT_SEASON else f"{dataset}_{season.replace('-', '_')}"
    )


def settings_for_season(settings: "Settings", season: str) -> "Settings":
    history = Path(settings.agent_history_path)
    history_path = (
        str(history.with_name(f"{history.stem}_{season}{history.suffix}"))
        if season != DEFAULT_SEASON
        else settings.agent_history_path
    )
    return replace(
        settings,
        agent_history_path=history_path,
        season=validate_season(season),
        gold_dataset=dataset_for_season(settings.gold_dataset, season),
        agent_dataset=dataset_for_season(settings.agent_dataset, season),
        metadata_dataset=dataset_for_season(settings.metadata_dataset, season),
    )
