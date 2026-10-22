"""Explicit live tests in a fresh synthetic-only BigQuery dev dataset."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.gamebook_cloud import CloudStore  # noqa: E402
from scripts.gamebook_store import canonical, require, sha  # noqa: E402

from gamebook_storage_scenarios import GAME, candidate  # noqa: E402


def run(output):
    output.mkdir(parents=True, exist_ok=False)
    original = candidate(output / "original")
    correction = candidate(output / "correction", assisted=False)
    rejected = candidate(output / "rejected", box_assists=2)
    fault = copy.deepcopy(original)
    extraction = fault[0]["extraction"]
    extraction["parser_version"] = "synthetic_fault_test/2"
    extraction["parser_sha256"] = "e" * 64
    extraction["revision"] = sha(
        canonical({k: v for k, v in extraction.items() if k != "revision"}).encode()
    )
    for e in fault[0]["scoring_events"]:
        e["revision"] = extraction["revision"]
        e["event_id"] = sha(
            f"{e['revision']}:{e['page']}:{e['row']}:{e['team']}".encode()
        )
    dataset = "nba_gamebook_dev_test_" + uuid4().hex[:12]
    store = CloudStore(
        os.environ["GCP_PROJECT_ID"],
        dataset,
        os.environ["GCS_BUCKET_NAME"],
        output / "cloud",
        os.getenv("BQ_LOCATION", "US"),
    )
    store.setup()
    store.register([GAME])
    steps = []
    for name, data, status, current, options in (
        ("initial", original, "published", 1, {}),
        ("identical_replay", original, "noop", 1, {}),
        ("corrected_game", correction, "published", 0, {}),
        ("rejected_replacement", rejected, "rejected", 0, {}),
        ("transaction_rollback", fault, "failed", 0, {"inject_failure": True}),
        ("replay_after_failure", correction, "noop", 0, {}),
        (
            "stale_publisher",
            fault,
            "failed",
            0,
            {"expected_previous": original[0]["extraction"]["revision"]},
        ),
    ):
        outcome = store.ingest(
            data[0],
            pdf=data[1],
            identity=data[2],
            reason="synthetic cloud scenario: " + name,
            **options,
        )
        snapshot = store.snapshot(name)
        counts = {k: len(v) for k, v in snapshot.items()}
        assert outcome["status"] == status, outcome
        assert len(snapshot["current_event_relationships"]) == current
        assert len(snapshot["gamebook_publication_history"]) == (
            1 if name in ("initial", "identical_replay") else 2
        )
        assert len(snapshot["gamebook_revisions"]) == (
            1 if name in ("initial", "identical_replay") else 2
        )
        if name not in ("initial", "identical_replay"):
            assert (
                snapshot["gamebook_coverage"][0]["current_revision"]
                == correction[0]["extraction"]["revision"]
            )
        if name in (
            "rejected_replacement",
            "transaction_rollback",
            "replay_after_failure",
            "stale_publisher",
        ):
            assert snapshot["gamebook_coverage"][0]["health"] == "failed"
        steps.append({"scenario": name, "outcome": outcome, "counts": counts})
        print(json.dumps(steps[-1]), flush=True)
    # Older immutable revisions and their saved report remain available.
    initial = json.loads((output / "cloud/initial.json").read_text())
    assert len(initial["current_event_relationships"]) == 1
    assert len(snapshot["event_relationships"]) == 1
    result = {
        "passed": True,
        "dataset": store.destination,
        "synthetic_only": True,
        "steps": steps,
    }
    (output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    require(
        args.output.resolve().is_relative_to((root / "reports").resolve()),
        "Output must be inside reports/",
    )
    load_dotenv(root / ".env")
    run(args.output)
