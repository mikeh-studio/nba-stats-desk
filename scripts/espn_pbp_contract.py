"""Pure ESPN PBP normalization/validation, shared with offline checks."""

from scripts import espn_sources as source
from scripts.espn_payload import source_input
from scripts.gamebook_store import canonical, require
from scripts.parse_gamebooks import sha

VERSION = "espn-only/1"


def normalize(payload, game):
    captured = source_input(payload)
    candidate = source.normalize_summary(captured, game, [])
    candidate.update(
        schema_version=VERSION,
        nba_game_id=None,
        reconciliation_status="reconciled",
        pdf_validation={"status": "not_attempted", "matches": []},
        reconciliation_input=captured,
    )
    return candidate


def validate(candidate):
    require(candidate["schema_version"] == VERSION, "Wrong ESPN-only schema")
    # Recompute from preserved PBP and box inputs: do not trust status flags/counts.
    expected = normalize(candidate["reconciliation_input"], candidate["game"])
    require(candidate == expected, "ESPN-only candidate differs from recomputed source")
    return sha(canonical(candidate).encode())
