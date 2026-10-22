"""Opt-in synthetic inputs against real isolated ESPN-only GCS/BigQuery storage."""

import argparse
import copy
import json
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from dotenv import load_dotenv  # noqa: E402
from scripts.espn_only_ingestion import make_store  # noqa: E402
from scripts.espn_only_store import normalize, validate  # noqa: E402
from scripts.fetch_gamebooks import write_json  # noqa: E402

from test_espn_only import candidate  # noqa: E402


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
    store = make_store(
        args.output / "cloud", "nba_gamebook_dev_espn_test_" + uuid4().hex[:12]
    )
    data = candidate()
    store.register(data["game"])
    evidence = store.cloud.upload(b'{"synthetic":true}', "espn_only_test", "json")
    results = []

    def run(name, value, expected, inject=False):
        actual = store.publish(value, evidence, inject_failure=inject)
        assert actual["status"] == expected, (name, actual)
        results.append({"scenario": name, **actual})
        write_json(args.output / f"progress-{len(results)}.json", results)

    def state():
        return store.cloud.query(f"SELECT * FROM `{store.prefix}.espn_only_games`")[0]

    run("initial", data, "published")
    run("identical_replay", data, "noop")
    assert (
        state()["health"] == "healthy"
        and state()["nba_game_mapping_status"] == "unresolved"
    )
    players = [
        {
            "source_player_id": "100",
            "nba_player_id": "10",
            "normalized_name": "avery able",
            "method": "synthetic",
            "catalog_sha256": "synthetic",
        }
    ]
    assert store.remember_players(players, evidence)["status"] == "published"
    run("replay_after_identity_enrichment", data, "noop")
    assert state()["nba_player_mapping_status"] == "partial"
    # Synthetic verification status models a successful comparison; not real PDF evidence.
    store.record_check(data, "pdf", "matched", evidence)
    store.record_check(data, "pdf", "unavailable", evidence, "synthetic network outage")
    assert (
        state()["pdf_verification_status"] == "matched"
        and state()["latest_pdf_attempt_status"] == "unavailable"
    )
    edited = copy.deepcopy(data["reconciliation_input"])
    edited["plays"][0]["clock"]["displayValue"] = "10:59"
    correction = normalize(edited, data["game"])
    run("valid_event_correction", correction, "published")
    assert state()["pdf_verification_status"] == "not_attempted"
    invalid = copy.deepcopy(correction)
    invalid["assists"] = []
    run("invalid_replacement", invalid, "rejected")
    edited["plays"][0]["clock"]["displayValue"] = "10:58"
    failed = normalize(edited, data["game"])
    run("transaction_rollback", failed, "rejected", True)
    run("noop_retains_failed_health", correction, "noop")
    run("historical_rollback_rejected", data, "rejected")
    retained = state()
    assert (
        retained["revision"] == validate(correction) and retained["health"] == "failed"
    )
    counts = store.cloud.query(
        f"SELECT (SELECT COUNT(*) FROM `{store.prefix}.espn_revisions`) revisions,(SELECT COUNT(*) FROM `{store.prefix}.espn_only_assists`) assists,(SELECT COUNT(*) FROM `{store.prefix}.espn_only_current_plays`) plays"
    )[0]
    assert counts == {"revisions": 2, "assists": 1, "plays": 4}, counts
    run("new_valid_revision_recovers_health", failed, "published")
    assert state()["health"] == "healthy" and state()["revision"] == validate(failed)
    report = {
        "scope": "Synthetic correction/failure inputs; actual isolated dev BigQuery transactions and GCS evidence",
        "dataset": store.prefix,
        "scenarios": results,
        "retained_after_failures": retained,
        "counts_after_failures": counts,
        "final_state": state(),
    }
    (args.output / "results.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n"
    )
    print(
        "PASS",
        len(results),
        "publication scenarios plus independent verification/identity checks",
        store.prefix,
    )


if __name__ == "__main__":
    main()
