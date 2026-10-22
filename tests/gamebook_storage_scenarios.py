"""Synthetic publication exercise; can save a private, inspectable local report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import parse_gamebooks as parse  # noqa: E402
from scripts.gamebook_store import GamebookStore  # noqa: E402

from test_gamebook_assists import (  # noqa: E402
    GAME,
    box_page,
    document,
    fragment,
    header,
    pdf_from_pages,
)


def candidate(directory, *, assisted=True, box_assists=None):
    directory.mkdir(parents=True, exist_ok=True)
    path, attempt, identity_path = document(directory)
    total = int(assisted) if box_assists is None else box_assists
    pages = [box_page(total)]
    for period in range(1, 5):
        rows = header(period) + [
            fragment(10, 90, "12:00"),
            fragment(200, 90, "Start of Period (7:00 PM)"),
        ]
        if period == 1:
            text = "B.Baker 25' 3PT Jump Shot" + (" (A.Able)" if assisted else "")
            rows += [
                fragment(10, 110, "11:00"),
                fragment(60, 110, text),
                fragment(280, 110, "3-0"),
            ]
        rows += [
            fragment(200, 150, f"End of {period}st QUARTER (3-0)"),
            fragment(200, 165, "End of Period (8:00 PM)"),
        ]
        pages.append(rows)
    body = pdf_from_pages(pages)
    path.write_bytes(body)
    attempt["sha256"] = parse.sha(body)
    return (
        parse.parse_document(path, attempt, identity_path),
        body,
        identity_path.read_bytes(),
    )


def ingest(store, data, **kwargs):
    result, pdf, identity = data
    return store.ingest(result, pdf=pdf, identity=identity, **kwargs)


def counts(store):
    return {
        name: store.rows(f"SELECT count(*) AS n FROM {name}")[0]["n"]
        for name in (
            "gamebook_documents",
            "gamebook_revisions",
            "gamebook_events",
            "event_relationships",
            "gamebook_extraction_runs",
            "gamebook_publication_history",
            "current_gamebook_events",
            "current_event_relationships",
        )
    }


def run_scenarios(output):
    output.mkdir(parents=True, exist_ok=False)
    original = candidate(output / "synthetic-original")
    corrected = candidate(output / "synthetic-corrected", assisted=False)
    invalid = candidate(output / "synthetic-rejected", box_assists=2)
    store = GamebookStore(output / "store")
    store.register_games([GAME])
    report = {
        "scope": "synthetic PDF publication tests; no actual NBA correction",
        "steps": [],
    }
    try:
        for name, data, expected_status, expected_current in (
            ("initial", original, "published", 1),
            ("identical_replay", original, "noop", 1),
            ("corrected_game", corrected, "published", 0),
            ("failed_replacement", invalid, "rejected", 0),
            ("replay_after_failure", corrected, "noop", 0),
        ):
            outcome = ingest(store, data, reason=f"synthetic exercise: {name}")
            state, totals = store.coverage(GAME["game_id"]), counts(store)
            assert outcome["status"] == expected_status, outcome
            assert totals["current_event_relationships"] == expected_current
            assert totals["current_gamebook_events"] == 1
            report["steps"].append(
                {
                    "scenario": name,
                    "outcome": outcome,
                    "counts": totals,
                    "coverage": state,
                }
            )
            store.export(output / f"export-{name}")
        assert counts(store)["gamebook_publication_history"] == 2
        assert counts(store)["gamebook_revisions"] == 2
        assert store.coverage(GAME["game_id"])["health"] == "failed"
        assert (
            store.coverage(GAME["game_id"])["current_revision"]
            == corrected[0]["extraction"]["revision"]
        )
        # Historical facts remain queryable; the original report stays pinned.
        assert store.rows("SELECT assists FROM assist_pair_game")[0]["assists"] == 1
        saved = json.loads((output / "export-initial/pair_report.json").read_text())
        assert saved["pairs"][0]["assists"] == 1
        report["passed"] = True
        (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
        (output / "report.md").write_text(
            "# Local storage publication exercise\n\n"
            "Synthetic PDFs only. This is not evidence of an actual NBA correction.\n\n"
            "| Scenario | Outcome | Current assists | Retained revisions |\n| --- | --- | ---: | ---: |\n"
            + "".join(
                f"| {s['scenario']} | {s['outcome']['status']} | {s['counts']['current_event_relationships']} | {s['counts']['gamebook_revisions']} |\n"
                for s in report["steps"]
            )
            + "\nThe correction removed an assist while preserving the made three. The rejected replacement claimed two box-score assists but supplied one assisted basket. The previous revision remains current; replaying it does not clear the failure. Original report membership and historical facts remain available.\n"
        )
        return report
    finally:
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if not args.output.resolve().is_relative_to((root / "reports").resolve()):
        parser.error("output must be a new directory inside reports/")
    result = run_scenarios(args.output)
    print(
        json.dumps(
            {"passed": result["passed"], "report": str(args.output / "report.md")}
        )
    )
