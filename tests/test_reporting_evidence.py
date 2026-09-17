"""Evidence boundaries and integration with deterministic Ask overviews."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.agent.reporting_evidence import (
    attach_reporting,
    digest,
    prepare_case,
    retrieve_reporting,
    validate_corpus,
    validate_response,
)
from app.agent.semantics import COMPONENTS, Evidence
from scripts.evaluate_reporting_evidence import (
    DIMENSIONS,
    REPORT_TEMPLATE,
    load_run,
    report,
    save,
    validate_batch,
)
from scripts.reporting_step_review import (
    audit_statistics,
    measured_usage,
    render_step_review,
)


@pytest.fixture
def case():
    return dict(
        id="E01",
        player_id=1,
        player_name="Example Player",
        start="2025-02-01",
        end="2025-02-28",
        previous_start="2025-01-01",
        previous_end="2025-01-31",
        seasons=["2024-25"],
        phase="Regular Season",
        reporting_mode="published_by_cutoff",
        reporting_cutoff="2025-02-28",
        question="Explain Example Player performance changes and team context.",
        category="synthetic contract test",
        expectations="Use the supplied evidence.",
    )


@pytest.fixture
def document():
    summary = "Example Player changed teams in February."
    return dict(
        id="R1",
        title="Example transaction",
        url="https://example.com/report",
        publisher="Example",
        author="Test author",
        summary=summary,
        retrieved_at="2026-09-16T00:00:00Z",
        version_available_date="2025-02-02",
        event_start="2025-02-01",
        event_end="2025-02-01",
        player_ids=[1],
        content_kind="curated_paraphrase",
        status="available",
        content_sha256=digest(summary),
    )


@pytest.fixture
def evidence():
    rows = []
    for day, points in (("2025-01-05", 10), ("2025-01-10", 20), ("2025-02-05", 30)):
        rows.append(
            {
                **dict.fromkeys(COMPONENTS, 0),
                "pts": points,
                "season": "2024-25",
                "season_type": "Regular Season",
                "game_id": day,
                "game_date": day,
                "player_id": 1,
                "player_name": "Example Player",
                "team_abbr": "ATL",
                "opponent_abbr": "BOS",
            }
        )
    scope = ("2024-25", "Regular Season")
    return Evidence(
        rows,
        frozenset([scope]),
        "synthetic",
        "test-snapshot",
        {scope: "2025-04-13"},
        complete=True,
    )


def test_real_overview_math_is_used(evidence, document, case):
    prepared = prepare_case(evidence, [document], case)
    metric = prepared["bundle"]["statistics"]["metrics"][0]
    assert (metric["current"], metric["previous"], metric["change"]) == (30, 15, 15)
    assert prepared["overview"]["tables"][0]["rows"][0][1] == "30.0"
    assert prepared["bundle"]["statistics"]["appearances"] == {
        "id": "S_games",
        "current": 1,
        "previous": 2,
    }


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("player_ids", [2], "different_player"),
        ("status", "withdrawn", "withdrawn"),
        ("version_available_date", "2025-03-01", "published_after_cutoff"),
    ],
)
def test_excluded_evidence_never_reaches_generator(
    document, case, field, value, reason
):
    document[field] = value
    result = retrieve_reporting([document], case)
    assert result["passages"] == []
    assert result["excluded"] == [{"id": "R1", "reason": reason}]


def test_wrong_season_event_rejected_even_if_recently_published(document, case):
    document.update(event_start="2024-02-01", event_end="2024-02-01")
    result = retrieve_reporting([document], case)
    assert result["excluded"][0]["reason"] == "outside_event_window"


def test_retrospective_mode_allows_later_report_on_matching_event(document, case):
    document["version_available_date"] = "2025-03-01"
    case["reporting_mode"] = "retrospective"
    assert retrieve_reporting([document], case)["passages"][0]["id"] == "R1"


def test_publication_day_boundary_is_inclusive(document, case):
    document["version_available_date"] = case["reporting_cutoff"]
    assert retrieve_reporting([document], case)["status"] == "found"


def test_duplicate_summaries_are_not_independent_corroboration(document, case):
    copy = {**document, "id": "R2", "url": "https://example.com/syndicated"}
    result = retrieve_reporting([document, copy], case)
    assert len(result["passages"]) == 1
    assert result["excluded"][-1]["reason"] == "duplicate_content"


def test_conflicting_reports_are_preserved_for_interpretation(document, case):
    copy = {**document, "id": "R2", "summary": "Example Player has not changed teams."}
    copy["content_sha256"] = digest(copy["summary"])
    assert len(retrieve_reporting([document, copy], case)["passages"]) == 2


@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", "http://example.com", "https://user:secret@example.com"],
)
def test_unsafe_source_urls_rejected(document, url):
    document["url"] = url
    with pytest.raises(ValueError, match="HTTPS"):
        validate_corpus([document])


def test_corpus_content_tampering_rejected(document):
    document["summary"] += " Unsupported extra fact."
    with pytest.raises(ValueError, match="checksum"):
        validate_corpus([document])


def test_identity_and_missing_coverage_rejected(evidence, document, case):
    with pytest.raises(ValueError, match="identity"):
        prepare_case(evidence, [document], {**case, "player_id": 2})
    with pytest.raises(ValueError, match="absent"):
        prepare_case(evidence, [document], {**case, "seasons": ["2023-24"]})


def test_overlapping_windows_rejected(evidence, document, case):
    with pytest.raises(ValueError, match="non-overlapping"):
        prepare_case(evidence, [document], {**case, "previous_end": "2025-02-02"})


def test_no_appearances_are_unavailable_not_zero(evidence, case):
    case.update(start="2025-03-01", end="2025-03-31", reporting_cutoff="2025-03-31")
    result = prepare_case(evidence, [], case)
    assert result["overview"]["status"] == "no_observations"
    assert result["bundle"]["statistics"]["metrics"][0]["current"] is None
    assert result["bundle"]["statistics"]["metrics"][0]["change"] is None
    assert result["bundle"]["statistics"]["appearances"]["current"] == 0


def test_missing_metric_withholds_change(evidence, case):
    evidence.rows[-1]["pts"] = None
    result = prepare_case(evidence, [], case)
    assert result["bundle"]["statistics"]["metrics"][0]["change"] is None


def test_unknown_citation_falls_back_without_mutating_overview(
    evidence, document, case
):
    p = prepare_case(evidence, [document], case)
    original = deepcopy(p["overview"])
    response = {
        "case_id": "E01",
        "claims": [
            {"kind": "reported_context", "text": "Unsupported", "evidence_ids": ["R99"]}
        ],
    }
    payload = attach_reporting(p["overview"], p["bundle"], response)
    assert payload["answer"] == original["answer"]
    assert payload["reporting_evidence"]["status"] == "invalid_response"
    assert p["overview"] == original


def test_valid_context_keeps_tables_charts_and_semantic_evidence(
    evidence, document, case
):
    p = prepare_case(evidence, [document], case)
    response = {
        "case_id": "E01",
        "claims": [
            {
                "kind": "reported_context",
                "text": "Example reports a team change.",
                "evidence_ids": ["R1"],
            }
        ],
    }
    payload = attach_reporting(p["overview"], p["bundle"], response)
    assert "https://example.com/report" in payload["answer"]
    for key in ("tables", "charts", "semantic_evidence", "player_profile"):
        assert payload[key] == p["overview"][key]


def test_citations_cannot_cross_evidence_types(evidence, document, case):
    b = prepare_case(evidence, [document], case)["bundle"]
    response = {
        "case_id": "E01",
        "claims": [{"kind": "statistic", "text": "30 points", "evidence_ids": ["R1"]}],
    }
    assert "claim_0:statistic_requires_metrics" in validate_response(b, response)


def test_batch_requires_exact_case_ids_once_in_order():
    with pytest.raises(ValueError, match="once, in order"):
        validate_batch(
            {"responses": [{"case_id": "E01"}, {"case_id": "E01"}]},
            "responses",
            ["E01", "E02"],
        )


def test_evaluator_cannot_pass_failed_dimensions():
    evaluation = dict(
        case_id="E01", verdict="pass", scores=dict.fromkeys(DIMENSIONS, 2), issues=[]
    )
    evaluation["scores"]["citation_support"] = 0
    with pytest.raises(ValueError, match="contradicts"):
        validate_batch({"evaluations": [evaluation]}, "evaluations", ["E01"])


def test_prepared_bundle_tampering_stops_run(tmp_path):
    save(tmp_path / "manifest.json", {"prepared_sha256": digest([])})
    save(tmp_path / "prepared.json", ["tampered"])
    with pytest.raises(ValueError, match="changed"):
        load_run(tmp_path)


def test_report_escapes_model_text_and_hides_judge(tmp_path, evidence, document, case):
    prepared = [prepare_case(evidence, [document], case)]
    save(
        tmp_path / "manifest.json",
        {"prepared_sha256": digest(prepared), "limits": ["Pilot"]},
    )
    save(tmp_path / "prepared.json", prepared)
    save(
        tmp_path / "generation.json",
        {
            "responses": [
                {
                    "case_id": "E01",
                    "claims": [
                        {
                            "kind": "limitation",
                            "text": '<script>alert("x")</script>',
                            "evidence_ids": [],
                        }
                    ],
                }
            ]
        },
    )
    save(
        tmp_path / "evaluation.json",
        {
            "evaluations": [
                dict(
                    case_id="E01",
                    verdict="revise",
                    scores=dict.fromkeys(DIMENSIONS, 1),
                    issues=[],
                    summary="Review",
                    strengths=[],
                )
            ]
        },
    )
    save(
        tmp_path / "review-notes.json",
        [{"case_id": "E01", "message": "Independent finding <unsafe>"}],
    )
    report(SimpleNamespace(run_dir=tmp_path))
    output = (tmp_path / "review.html").read_text()
    assert '<script>alert("x")</script>' not in output
    assert "&lt;script&gt;" in output
    assert '<details class="judge">' in output
    assert '<details class="judge" open' not in output
    assert "Independent finding &lt;unsafe&gt;" in output
    assert "Additional verification" in output


@pytest.fixture
def recorded_usage(tmp_path):
    usage = dict(
        input_tokens=100,
        output_tokens=30,
        cached_input_tokens=60,
        reasoning_output_tokens=10,
    )
    output = {"responses": []}
    save(tmp_path / "generation.json", output)
    (tmp_path / "generation-prompt.txt").write_text("Frozen prompt")
    save(
        tmp_path / "generation-metadata.json",
        dict(
            usage=[usage],
            prompt_sha256=digest("Frozen prompt"),
            exit_code=0,
            tool_items=[],
            started_at="2026-09-16T00:00:00+00:00",
            ended_at="2026-09-16T00:00:02+00:00",
            requested_model="gpt-5.6-luna",
        ),
    )
    events = [
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps(output)},
        },
        {"type": "turn.completed", "usage": usage},
    ]
    (tmp_path / "generation-events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events)
    )
    return tmp_path


def test_usage_does_not_double_count_cached_or_reasoning(recorded_usage):
    result = measured_usage(recorded_usage, "generation")
    assert result["total"] == 130
    assert result["cached_input_tokens"] == 60
    assert result["reasoning_output_tokens"] == 10
    assert result["per_response_tokens"] is None
    assert result["duration_seconds"] == 2
    assert result["checks"] == []


def test_usage_requires_matching_raw_events(recorded_usage):
    path = recorded_usage / "generation-metadata.json"
    metadata = json.loads(path.read_text())
    metadata["usage"][0]["input_tokens"] += 1
    path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="disagree"):
        measured_usage(recorded_usage, "generation")


def test_usage_flags_altered_prompt_and_output(recorded_usage):
    (recorded_usage / "generation-prompt.txt").write_text("Changed")
    (recorded_usage / "generation.json").write_text(
        json.dumps({"responses": ["Changed"]})
    )
    assert len(measured_usage(recorded_usage, "generation")["checks"]) == 2


def test_absent_usage_is_unknown_not_zero(tmp_path):
    assert measured_usage(tmp_path, "generation")["total"] is None


def test_independent_audit_detects_altered_statistics(evidence, case):
    prepared = [prepare_case(evidence, [], case)]
    assert audit_statistics({"rows": evidence.rows}, prepared)[0]["errors"] == []
    prepared[0]["bundle"]["statistics"]["metrics"][0]["change"] = 999
    assert (
        "pts.change"
        in audit_statistics({"rows": evidence.rows}, prepared)[0]["errors"][0]
    )


def test_independent_audit_preserves_missingness(evidence, case):
    evidence.rows[-1]["pts"] = None
    prepared = [prepare_case(evidence, [], case)]
    assert audit_statistics({"rows": evidence.rows}, prepared)[0]["errors"] == []
    prepared[0]["bundle"]["statistics"]["metrics"][0]["current"] = 0
    assert audit_statistics({"rows": evidence.rows}, prepared)[0]["errors"]


def test_step_review_escapes_evidence_and_separates_notes(tmp_path, monkeypatch):
    audit = dict(
        run_id="frozen",
        all_model_usage_available=True,
        measured_model_tokens=0,
        token_scope="Setup excluded",
        cost_note="No dollar estimate",
        accounting="Input + output",
        steps=[
            dict(
                id="S01",
                title="Scope",
                tokens=None,
                status="Review",
                result="Review this",
                criterion="Check",
                limitation="Pilot",
                inputs="Questions",
                outputs="Cases",
                artifacts=["cases.json"],
                details=["<script>unsafe</script>"],
            )
        ],
    )
    monkeypatch.setattr(
        "scripts.reporting_step_review.build_step_audit", lambda *args: audit
    )
    render_step_review(tmp_path, {}, [], [], [], REPORT_TEMPLATE)
    page = (tmp_path / "step-review.html").read_text()
    assert "&lt;script&gt;unsafe&lt;/script&gt;" in page
    assert "<script>unsafe</script>" not in page
    assert "nba-evidence-step-review-" in page
    assert "nba-evidence-human-review.json" not in page
    assert 'data-case="S01"' in page
    assert "Unmetered setup" in page


def test_composite_usage_counts_both_calls_and_rejects_modified_output(recorded_usage):
    import shutil

    second = recorded_usage / "second"
    second.mkdir()
    for file in recorded_usage.glob("generation*"):
        if file.is_file():
            shutil.copy(file, second / file.name)
    combined = recorded_usage / "combined"
    combined.mkdir()
    save(combined / "generation.json", {"responses": []})
    save(
        combined / "generation-metadata.json",
        {
            "composition": "concatenate_responses",
            "parts": [
                {"run_dir": str(recorded_usage), "stage": "generation"},
                {"run_dir": str(second), "stage": "generation"},
            ],
        },
    )
    usage = measured_usage(combined, "generation")
    assert usage["total"] == 260
    assert usage["completed_turns"] == 2
    (combined / "generation.json").write_text(json.dumps({"responses": ["altered"]}))
    with pytest.raises(ValueError, match="differ"):
        measured_usage(combined, "generation")
