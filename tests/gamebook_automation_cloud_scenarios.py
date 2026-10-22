"""Opt-in synthetic source tests of the real unattended dev cloud worker."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from dotenv import load_dotenv  # noqa: E402
from scripts import gamebook_ingestion as worker  # noqa: E402
from scripts.gamebook_store import require  # noqa: E402

from test_gamebook_ingestion import (  # noqa: E402
    AUTO_GAME,
    game_session,
    synthetic_sources,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(
        args.output.resolve().is_relative_to(ROOT / "reports")
        and not args.output.exists(),
        "Use a new reports/ directory",
    )
    args.output.mkdir(parents=True)
    load_dotenv(ROOT / ".env")
    dataset = "nba_gamebook_dev_test_automation_" + uuid4().hex[:12]
    os.environ["GAMEBOOK_DEV_DATASET"] = dataset
    original = synthetic_sources(args.output / "original")
    corrected = synthetic_sources(args.output / "corrected", assisted=False)
    invalid = synthetic_sources(args.output / "invalid", box_assists=2)
    steps = []
    for i, (name, data, status, count) in enumerate(
        (
            ("initial", original, "published", 1),
            ("replay", original, "noop", 1),
            ("correction", corrected, "published", 0),
            ("rejected_replacement", invalid, "rejected", 0),
        )
    ):
        output = args.output / name
        output.mkdir()
        store = worker.make_store(output / "cloud", AUTO_GAME["game_id"])
        # Exercise full-schedule registration SQL and a fresh checkpoint reader each attempt.
        worker.register_completed(store, [AUTO_GAME])
        outcome = worker.process_game(AUTO_GAME, output, store, game_session(*data))
        snapshot = store.snapshot("verified")
        assert outcome["status"] == status, outcome
        assert len(snapshot["current_event_relationships"]) == count
        assert len(snapshot["gamebook_publication_history"]) == (1 if i < 2 else 2)
        steps.append({"name": name, "outcome": outcome, "current_assists": count})
        print(name, status, flush=True)
    assert snapshot["gamebook_coverage"][0]["health"] == "failed"
    result = {
        "dataset": dataset,
        "scope": "Real GCS/BigQuery worker execution with synthetic PDF/HTTP; not NBA source availability or scheduler deployment",
        "steps": steps,
    }
    (args.output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(args.output / "results.json", flush=True)


if __name__ == "__main__":
    main()
