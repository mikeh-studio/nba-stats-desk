from types import SimpleNamespace

import pytest
from scripts.stage_game_log_repair import (
    isolated_environment,
    raw_repaired_rows,
    validate_manifest,
)


def test_repair_environment_overrides_every_destination(tmp_path, monkeypatch):
    monkeypatch.setenv("BQ_DATASET_GOLD", "nba_gold")
    env = isolated_environment("test-project", "repair_012345abcdef", tmp_path)
    for layer in ("BRONZE", "SILVER", "GOLD", "AGENT"):
        assert env["BQ_DATASET_" + layer].endswith("_repair_012345abcdef")
    with pytest.raises(ValueError):
        isolated_environment("test-project", "main", tmp_path)


def test_manifest_escape_is_blocked():
    manifest = {
        "nodes": {"fact": {"resource_type": "model", "schema": "nba_gold"}},
        "sources": {},
    }
    with pytest.raises(ValueError, match="escaped"):
        validate_manifest(manifest, {"nba_gold_repair_012345abcdef"})


def test_repair_raw_projection_preserves_zero_attempts_and_rejects_unknown_fields():
    row = dict(pts=0, fgm=0, fga=0, fg3m=0, fg3a=0, ftm=0, fta=0, home_away="AWAY")
    snapshot = {"rows": [row], "source_timestamp": "2026-10-05T00:00:00Z"}
    columns = [*row, "fg_pct", "fg3_pct", "ft_pct", "ingested_at_utc"]
    schema = [SimpleNamespace(name=name) for name in columns]
    result = raw_repaired_rows(snapshot, schema)[0]
    assert result["fg_pct"] == 0
    assert result["home_away"] == "AWAY"
    with pytest.raises(ValueError, match="does not supply"):
        raw_repaired_rows(
            snapshot, [*schema, SimpleNamespace(name="unknown_new_component")]
        )
