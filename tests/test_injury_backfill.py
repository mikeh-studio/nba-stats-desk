from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import backfill_injury_reports as backfill


def test_build_candidate_plan_does_not_silently_truncate_window() -> None:
    candidates = backfill.build_candidate_plan(
        start_date="2026-05-01",
        end_date="2026-05-05",
        report_times_et=["05_00PM"],
        max_candidates=5,
        allow_large_window=False,
    )

    assert [candidate["report_date"].isoformat() for candidate in candidates] == [
        "2026-05-01",
        "2026-05-02",
        "2026-05-03",
        "2026-05-04",
        "2026-05-05",
    ]


def test_build_candidate_plan_refuses_large_window_without_override() -> None:
    try:
        backfill.build_candidate_plan(
            start_date="2026-05-01",
            end_date="2026-05-05",
            report_times_et=["05_00PM", "01_30PM"],
            max_candidates=5,
            allow_large_window=False,
        )
    except backfill.BackfillError as exc:
        assert "Refusing to process 10 injury-report candidates" in str(exc)
    else:
        raise AssertionError("Expected large candidate window to fail")


def test_candidate_summary_records_first_and_last_source_urls() -> None:
    candidates = backfill.build_candidate_plan(
        start_date="2026-05-01",
        end_date="2026-05-02",
        report_times_et=["05_00PM"],
        max_candidates=2,
        allow_large_window=False,
    )

    summary = backfill.candidate_summary(candidates)

    assert summary["candidate_count"] == 2
    assert summary["first_report_date"] == "2026-05-01"
    assert summary["last_report_date"] == "2026-05-02"
    assert summary["first_source_url"].endswith(
        "Injury-Report_2026-05-01_05_00PM.pdf"
    )
