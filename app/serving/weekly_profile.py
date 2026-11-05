"""Versioned rolling scope; v1 remains the unchanged October rehearsal profile."""

import copy
import json
from datetime import date
from pathlib import Path

BASE = json.loads(
    (
        Path(__file__).resolve().parents[2] / "contracts/publication/core-stats-v1.json"
    ).read_text()
)


def weekly_scope(value):
    if set(value) != {"season", "phase", "first", "last", "provider"}:
        raise ValueError("invalid rolling scope fields")
    if (
        value["provider"] not in {"espn", "nba_api"}
        or value["phase"] != "Regular Season"
    ):
        raise ValueError("unsupported provider/phase")
    season = value["season"]
    year = int(season[:4])
    if season != f"{year}-{str(year + 1)[-2:]}" or year != 2026:
        raise ValueError("unsupported season")
    first, last = date.fromisoformat(value["first"]), date.fromisoformat(value["last"])
    if not date(year, 10, 1) <= first <= last <= date(year + 1, 6, 30):
        raise ValueError("rolling dates outside season")
    return dict(value)


def profile_for(manifest):
    if manifest.get("version") == "publication-manifest/1":
        return BASE
    if (
        manifest.get("version") != "publication-manifest/2"
        or manifest.get("profile") != "core_stats_weekly_v1"
    ):
        raise ValueError("unsupported publication profile")
    result = copy.deepcopy(BASE)
    result["version"] = "core_stats_weekly_v1"
    result["scope"] = weekly_scope(manifest["scope"])
    return result
