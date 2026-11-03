"""Opt-in identity enrichment/replay/conflict tests in disposable cloud storage."""

import argparse
import copy
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from dotenv import load_dotenv  # noqa: E402
from scripts.espn_identity import crosswalk  # noqa: E402
from scripts.espn_ingestion import make_store  # noqa: E402
from scripts.espn_store import validate  # noqa: E402
from tests.support.espn import REF, candidate  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    out = parser.parse_args().output
    assert out.resolve().is_relative_to(ROOT / "reports") and not out.exists()
    out.mkdir(parents=True)
    load_dotenv(ROOT / ".env")
    os.environ["ESPN_DEV_DATASET"] = "nba_gamebook_dev_espn_test_" + uuid4().hex[:12]
    store = make_store(out / "cloud", "identity-scenarios")
    c = candidate()
    for p in c["players"]:
        p["nba_player_id"] = None
    for e in c["assists"]:
        e["scorer_nba_id"] = e["assister_nba_id"] = None
    c["pdf_validation"] = {"status": "pending_mapping", "matches": []}
    evidence = store.cloud.upload(b'{"synthetic":true}', "espn_test_evidence", "json")
    store.register(c["game"])
    assert store.publish(c, evidence)["status"] == "published"
    revision = validate(c)
    mappings, missing = crosswalk(
        c["players"], REF, {"method": "synthetic", "sha256": "synthetic"}
    )
    assert not missing
    outcomes = []
    first = store.remember_players(mappings, evidence)
    assert first == {"status": "published", "inserted": 3}
    outcomes.append({"scenario": "fill_missing_player_ids", **first})
    replay = store.remember_players(mappings, evidence)
    assert replay == {"status": "noop", "inserted": 0}
    outcomes.append({"scenario": "identical_crosswalk_replay", **replay})
    bad = copy.deepcopy(mappings)
    bad[0]["nba_player_id"] = "999"
    bad.append(
        {
            **mappings[0],
            "source_player_id": "9999",
            "nba_player_id": "9999",
            "normalized_name": "new player",
        }
    )
    rejected = store.remember_players(bad, evidence)
    assert rejected["status"] == "rejected"
    outcomes.append(
        {"scenario": "conflict_rolls_back_entire_crosswalk_batch", **rejected}
    )
    state = store.cloud.query(
        f"""SELECT c.current_revision,c.health,(SELECT COUNT(*) FROM `{store.prefix}.espn_player_mappings`) mappings,(SELECT COUNTIF(nba_player_id IS NULL) FROM `{store.prefix}.espn_current_player_box_scores`) raw_null_ids,(SELECT COUNTIF(nba_player_id IS NULL) FROM `{store.prefix}.espn_resolved_player_box_scores`) resolved_null_ids,(SELECT COUNTIF(assister_nba_id IS NULL OR scorer_nba_id IS NULL) FROM `{store.prefix}.espn_resolved_assists`) unresolved_assists FROM `{store.prefix}.espn_coverage` c"""
    )[0]
    assert state == {
        "current_revision": revision,
        "health": "pending_mapping",
        "mappings": 3,
        "raw_null_ids": 3,
        "resolved_null_ids": 0,
        "unresolved_assists": 0,
    }, state
    outcomes.append(
        {
            "scenario": "enrichment_preserves_fact_revision_and_validation_health",
            "status": "passed",
        }
    )
    changed = candidate()
    changed["players"][0]["nba_player_id"] = "999"
    for e in changed["assists"]:
        if e["assister_source_id"] == changed["players"][0]["source_player_id"]:
            e["assister_nba_id"] = "999"
        if e["scorer_source_id"] == changed["players"][0]["source_player_id"]:
            e["scorer_nba_id"] = "999"
    result = store.publish(changed, evidence)
    assert result["status"] == "rejected"
    outcomes.append(
        {"scenario": "fact_publication_cannot_override_crosswalk", **result}
    )
    assert (
        store.cloud.query(
            f"SELECT current_revision FROM `{store.prefix}.espn_coverage`"
        )[0]["current_revision"]
        == revision
    )
    report = {
        "scope": "Synthetic identity cases, actual isolated GCS/BigQuery writes",
        "dataset": store.prefix,
        "scenarios": outcomes,
        "state_before_rejected_fact": state,
    }
    (out / "results.json").write_text(json.dumps(report, indent=2, default=str))
    print("PASS: 5 identity cloud scenarios", store.prefix)


if __name__ == "__main__":
    main()
