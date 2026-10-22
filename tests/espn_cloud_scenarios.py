"""Opt-in real GCS/BQ replay/correction/rollback tests using synthetic inputs."""

from __future__ import annotations

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
from scripts.espn_ingestion import make_store  # noqa: E402
from scripts.espn_store import validate  # noqa: E402

from test_espn_pilot import candidate  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert (
        args.output.resolve().is_relative_to(ROOT / "reports")
        and not args.output.exists()
    )
    args.output.mkdir(parents=True)
    load_dotenv(ROOT / ".env")
    os.environ["ESPN_DEV_DATASET"] = "nba_gamebook_dev_espn_test_" + uuid4().hex[:12]
    store = make_store(args.output / "cloud", "scenarios")
    data = candidate()
    store.register(data["game"])
    evidence = store.cloud.upload(b'{"synthetic":true}', "espn_test_evidence", "json")
    results = []

    def run(name, value, expected, inject=False):
        result = store.publish(value, evidence, inject_failure=inject)
        assert result["status"] == expected, (name, result)
        results.append({"scenario": name, **result})

    run("initial", data, "published")
    run("identical_replay", data, "noop")
    corrected = copy.deepcopy(data)
    corrected["players"][0]["stats"]["STL"] += 1
    run("valid_correction", corrected, "published")
    invalid = copy.deepcopy(corrected)
    invalid["players"][0]["stats"]["AST"] += 1
    run("invalid_replacement", invalid, "rejected")
    failed = copy.deepcopy(corrected)
    failed["players"][0]["stats"]["STL"] += 1
    run("transaction_rollback", failed, "rejected", True)
    run("replay_retains_failure_health", corrected, "noop")
    run("historical_revision_rejected", data, "rejected")
    state = store.cloud.query(
        f"SELECT c.*, (SELECT COUNT(*) FROM `{store.prefix}.espn_revisions`) revisions,(SELECT COUNT(*) FROM `{store.prefix}.espn_current_assists`) assists FROM `{store.prefix}.espn_coverage` c"
    )[0]
    assert (
        state["current_revision"] == validate(corrected) and state["health"] == "failed"
    )
    assert state["revisions"] == 2 and state["assists"] == 1
    counts = store.cloud.query(
        f"SELECT COUNT(*) players,SUM(steals) steals FROM `{store.prefix}.espn_current_player_box_scores`"
    )[0]
    assert counts == {"players": 3, "steals": 1}, counts
    report = {
        "scope": "Synthetic candidates, actual dev GCS/BigQuery writes; no live provider corrections manufactured",
        "dataset": store.prefix,
        "scenarios": results,
        "final_state": state,
        "counts": counts,
    }
    (args.output / "results.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n"
    )
    print("PASS: 7 cloud scenarios", store.prefix)


if __name__ == "__main__":
    main()
