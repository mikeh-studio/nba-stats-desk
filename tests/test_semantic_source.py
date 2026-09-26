import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.agent.semantic_source import (
    BigQuerySemanticSource,
    snapshot_digest,
    snapshot_evidence,
)
from app.agent.semantics import (
    COMPONENTS,
    Query,
    SemanticError,
    compare_queries,
)
from google.api_core.exceptions import Forbidden, NotFound
from scripts.evaluate_historical_semantics import evaluate, freeze_references

FIXTURE = Path(__file__).parent / "fixtures/semantics/cases.json"


def source_rows():
    fixture = json.loads(FIXTURE.read_text())
    rows = fixture["rows"]
    for row in rows:
        row["player_name"] = {
            1: "Regular Player",
            2: "Small Sample",
            3: "Third Player",
        }[row["player_id"]]
    # A small complete source containing both phases for the adapter contract.
    return [r for r in rows if r["season"] == "2024-25"]


class Client:
    def __init__(self, rows=None, error=None, total=None):
        self.rows = source_rows() if rows is None else rows
        self.error = error
        self.total = len(self.rows) if total is None else total

    def query(self, sql, **kwargs):
        self.sql, self.config = sql, kwargs["job_config"]
        if self.error:
            raise self.error
        return SimpleNamespace(
            job_id="fixture-job",
            total_bytes_processed=100,
            total_bytes_billed=100,
            result=lambda **kw: [
                dict(r, source_row_count=self.total) for r in self.rows
            ],
        )


def test_capture_uses_fixed_parameterized_bounded_snapshot():
    client = Client()
    snapshot = BigQuerySemanticSource(client, project="test-project").capture(
        ["2024-25"]
    )
    assert snapshot_evidence(snapshot).complete
    assert "test-project.nba_gold_2024_25.fct_player_game_stats" in client.sql
    assert "FOR SYSTEM_TIME AS OF @snapshot_at" in client.sql
    assert "LIMIT @row_limit" in client.sql
    assert client.config.maximum_bytes_billed == 100_000_000
    assert client.config.use_query_cache is False
    assert snapshot["capture"]["query_id"] == "fixture-job"
    assert len(snapshot["rows"]) == 11


@pytest.mark.parametrize(
    "error, code",
    [(Forbidden("no"), "access_denied"), (NotFound("no"), "unsupported_coverage")],
)
def test_source_errors_never_become_empty_answers(error, code):
    with pytest.raises(SemanticError) as exc:
        BigQuerySemanticSource(Client(error=error), project="test-project").capture(
            ["2024-25"]
        )
    assert exc.value.code == code


@pytest.mark.parametrize(
    "client, max_rows",
    [(Client(total=100), 100), (Client(), 2), (Client(rows=[]), 100)],
)
def test_truncated_or_empty_source_is_blocked(client, max_rows):
    with pytest.raises(SemanticError, match="snapshot|truncated") as exc:
        BigQuerySemanticSource(
            client, project="test-project", max_rows=max_rows
        ).capture(["2024-25"])
    assert exc.value.code == (
        "unsupported_coverage" if not client.rows else "incomplete_evidence"
    )


def test_source_manifest_detects_tampering_and_missing_rows():
    snapshot = BigQuerySemanticSource(Client(), project="test-project").capture(
        ["2024-25"]
    )
    snapshot["rows"].pop()
    with pytest.raises(SemanticError, match="checksum"):
        snapshot_evidence(snapshot)
    snapshot["sha256"] = snapshot_digest(snapshot)
    with pytest.raises(SemanticError, match="counts"):
        snapshot_evidence(snapshot)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"project": "bad`;DROP TABLE x"},
        {"project": "test-project", "gold_dataset": "bad.dataset"},
        {"project": "test-project", "max_rows": 100001},
    ],
)
def test_source_rejects_identifiers_and_unbounded_requests(kwargs):
    with pytest.raises(SemanticError):
        BigQuerySemanticSource(Client(), **kwargs)


def test_comparison_reports_percentage_points_and_overlap():
    snapshot = BigQuerySemanticSource(Client(), project="test-project").capture(
        ["2024-25"]
    )
    evidence = snapshot_evidence(snapshot)
    current = Query(
        "fg_pct", "2024-25", "ratio", player_id=2, window="last_n_games", n=1
    )
    baseline = Query("fg_pct", "2024-25", "ratio", player_id=2)
    actual = compare_queries(evidence, current, baseline)
    assert actual["difference"] == -10
    assert actual["difference_unit"] == "percentage_points"
    assert actual["relative_change_pct"] == -100
    assert actual["overlapping_game_ids"] == ["009"]
    with pytest.raises(SemanticError, match="compatible"):
        compare_queries(
            evidence, current, Query("pts", "2024-25", "average", player_id=2)
        )


def test_historical_harness_freezes_independent_sql_and_detects_wrong_result(tmp_path):
    snapshot = BigQuerySemanticSource(Client(), project="test-project").capture(
        ["2024-25"]
    )
    # Capture includes exactly the fixed adapter schema.
    allowed = {
        "season",
        "season_type",
        "game_id",
        "game_date",
        "player_id",
        "player_name",
        "team_abbr",
        "opponent_abbr",
    } | COMPONENTS
    snapshot["rows"] = [
        {k: v for k, v in r.items() if k in allowed} for r in snapshot["rows"]
    ]
    snapshot["sha256"] = snapshot_digest(snapshot)
    source = tmp_path / "snapshot.json"
    source.write_text(json.dumps(snapshot))
    references = freeze_references(source)
    path = tmp_path / "references.json"
    path.write_text(json.dumps(references))
    report = evaluate(source, path)
    assert report["passed"] == report["total"] == 10
    assert report["release_ready"] is False
    changed = copy.deepcopy(references)
    changed["cases"][1]["expected"][0]["value"] += 10
    path.write_text(json.dumps(changed))
    report = evaluate(source, path)
    assert report["passed"] == report["total"] - 1
    assert report["results"][1]["failure_categories"] == ["metric_math"]


def test_regular_season_capture_does_not_require_playoff_rows():
    rows = [r for r in source_rows() if r["season_type"] == "Regular Season"]
    snapshot = BigQuerySemanticSource(
        Client(rows=rows), project="test-project"
    ).capture(["2024-25"])
    evidence = snapshot_evidence(snapshot)
    assert evidence.covered_scopes == frozenset({("2024-25", "Regular Season")})
