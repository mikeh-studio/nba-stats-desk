"""Validated season selection and isolated historical warehouse routing."""

import re
from contextvars import ContextVar
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from app.config import Settings

SEASON_FILE = Path(__file__).resolve().parents[1] / "season.yml"
FIRST_ARCHIVE_SEASON = "2023-24"


def _season_label(start_year: int) -> str:
    return f"{start_year}-{(start_year + 1) % 100:02d}"


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
    """Warehouse routing bounds, not opening night or the phase's game dates."""
    year = int(validate_season(season)[:4])
    return date(year, 7, 1), date(year + 1, 6, 30)


def current_season() -> str:
    return _selected_season.get()


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
