from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.build_gamebook_identity_map import build_map
from scripts.fetch_gamebook_identities import validate

GAME = {"season": "2026-27", "date": "2026-10-20", "home": "BBB", "away": "AAA"}
PLAYERS = [
    {"name": "Avery Able", "team": "BBB", "played": True},
    {"name": "Blake Baker", "team": "AAA", "played": True},
]
REFERENCE = {
    "source_table": "example.reference",
    "job_id": "synthetic-job",
    "rows": [
        {
            "player_id": "10",
            "player_name": "Avery Able",
            "ingested_at_utc": "2026-01-01T00:00:00Z",
        }
    ],
}
SUPPLEMENT = [
    {
        "name": "Blake Baker",
        "player_id": "11",
        "source_url": "https://www.nba.com/player/11/blake-baker",
        "reviewed_at": "2026-10-21",
        "reviewer": "synthetic-reviewer",
    }
]


def test_identity_map_is_explicit_and_preserves_source_attribution():
    result = build_map(GAME, PLAYERS, REFERENCE, SUPPLEMENT)
    assert result["stats"]["Home"]["FullGame"][0]["EntityId"] == "10"
    assert result["stats"]["Away"]["FullGame"][0]["EntityId"] == "11"
    assert "not a provider game-stats response" in result["scope_note"]
    assert (
        result["stats"]["Away"]["FullGame"][0]["identity_evidence"]["source_url"]
        == SUPPLEMENT[0]["source_url"]
    )
    validate(result, GAME)


def test_unresolved_and_ambiguous_names_fail_closed():
    with pytest.raises(ValueError, match="Unresolved"):
        build_map(GAME, PLAYERS, REFERENCE, [])
    ref = copy.deepcopy(REFERENCE)
    ref["rows"].append({**ref["rows"][0], "player_id": "12"})
    with pytest.raises(ValueError, match="ambiguous"):
        build_map(GAME, PLAYERS, ref, SUPPLEMENT)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_url", "https://example.test/player/11"),
        ("source_url", "https://www.nba.com/player/12"),
        ("reviewer", ""),
    ],
)
def test_supplement_requires_matching_official_id_and_attribution(field, value):
    data = copy.deepcopy(SUPPLEMENT)
    data[0][field] = value
    with pytest.raises(ValueError):
        build_map(GAME, PLAYERS, REFERENCE, data)


def test_game_specific_provider_reference_rejects_wrong_season():
    result = build_map(GAME, PLAYERS, REFERENCE, SUPPLEMENT)
    result["season"] = "2025-26"
    with pytest.raises(ValueError, match="date/season/phase"):
        validate(result, GAME)
