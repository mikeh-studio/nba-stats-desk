from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from app.agent.semantic_source import BigQuerySemanticSource, snapshot_evidence
from app.agent.semantics import SemanticError
from app.agent.serving_publication import (
    candidate_manifest,
    candidate_summaries,
    validate_publication,
)
from tests.test_semantic_source import Client

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


@pytest.fixture
def publication():
    from app.agent.semantic_source import snapshot_digest

    snapshot = BigQuerySemanticSource(Client(), project="test-project").capture(
        ["2024-25"]
    )
    snapshot["source_timestamp"] = "2026-10-21T23:59:00+00:00"
    snapshot["sha256"] = snapshot_digest(snapshot)
    source = snapshot_evidence(snapshot)
    rows = candidate_summaries(source, "pub-a", ["pts", "fg_pct"])
    manifest = candidate_manifest(
        source,
        rows,
        publication_id="pub-a",
        metrics=["pts", "fg_pct"],
        built_at="2026-10-21T23:59:30+00:00",
        expires_at="2026-10-22T00:04:00+00:00",
        relation_version="immutable-table-a",
        source_timestamp=snapshot["source_timestamp"],
    )
    return source, rows, manifest, snapshot


def check(source, rows, manifest, snapshot):
    return validate_publication(
        manifest,
        rows,
        source=source,
        source_snapshot=snapshot,
        expected_snapshot_id=source.snapshot_id,
        now=NOW,
    )


def test_valid_publication_and_exact_capability(publication):
    source, rows, manifest, snapshot = publication
    result = check(source, rows, manifest, snapshot)
    from app.agent.semantics import Query

    query = Query(metric="pts", season="2024-25", aggregation="average", player_id=1)
    assert result.supports(query, now=NOW)
    for change in [
        dict(opponent_abbr="AAA"),
        dict(home_away="HOME"),
        dict(as_of="2025-04-01"),
        dict(min_games=20),
        dict(player_id=None),
        dict(seasons=["2023-24", "2024-25"]),
        dict(metric="fg_pct"),
        dict(operation="rank"),
        dict(season="2023-24"),
    ]:
        assert not result.supports(replace(query, **change), now=NOW)
    assert not result.supports(query, now=datetime(2026, 10, 23, tzinfo=timezone.utc))
    result.verify_binding(rows, relation_version="immutable-table-a", now=NOW)
    with pytest.raises(SemanticError):
        result.verify_binding(rows[:-1], relation_version="immutable-table-a", now=NOW)
    with pytest.raises(SemanticError):
        result.verify_binding(rows, relation_version="replaced-table", now=NOW)


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "mixed", "components", "membership", "phase"]
)
def test_corrupt_rows_even_with_recomputed_candidate_digest(publication, mutation):
    source, original, _, snapshot = publication
    rows = list(original)
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(rows[0])
    elif mutation == "mixed":
        rows[0] = replace(rows[0], publication_id="pub-b")
    elif mutation == "components":
        rows[0] = replace(rows[0], components={"pts": 10000})
    elif mutation == "membership":
        rows[0] = replace(rows[0], game_ids=("invented",))
    else:
        rows = [r for r in rows if r.phase != "Playoffs"]
    manifest = candidate_manifest(
        source,
        rows,
        publication_id="pub-a",
        metrics=["pts", "fg_pct"],
        built_at="2026-10-21T23:59:30+00:00",
        expires_at="2026-10-22T00:04:00+00:00",
        relation_version="immutable-table-a",
        source_timestamp=snapshot["source_timestamp"],
    )
    with pytest.raises(SemanticError, match="failed validation"):
        check(source, rows, manifest, snapshot)


@pytest.mark.parametrize(
    "field,value",
    [
        ("contract_version", "wrong"),
        ("policy_version", "wrong"),
        ("source_snapshot_id", "other"),
        ("summary_count", 0),
        ("validation_status", "approved"),
        ("expires_at", "2026-10-22T00:00:00+00:00"),
        ("built_at", "2026-10-23T00:00:00+00:00"),
        ("built_at", "2026-10-21"),
        ("relation_version", ""),
    ],
)
def test_manifest_and_freshness_failures(publication, field, value):
    source, rows, original, snapshot = publication
    manifest = deepcopy(original)
    manifest[field] = value
    with pytest.raises(SemanticError):
        check(source, rows, manifest, snapshot)


def test_scope_membership_and_corrected_source_must_match(publication):
    source, rows, manifest, snapshot = publication
    bad = deepcopy(manifest)
    bad["scopes"][0]["player_ids"].pop()
    with pytest.raises(SemanticError):
        check(source, rows, bad, snapshot)
    with pytest.raises(SemanticError):
        validate_publication(
            manifest,
            rows,
            source=source,
            source_snapshot=snapshot,
            expected_snapshot_id="corrected-source",
            now=NOW,
        )
    with pytest.raises(SemanticError):
        check(replace(source, cohort_complete=False), rows, manifest, snapshot)


@pytest.mark.parametrize("variant", ["complete", "missing_points", "ambiguous"])
def test_existing_endpoint_harness_frozen_source_parity(variant):
    import json
    from pathlib import Path

    from scripts.ask_eval.serving import evaluate

    source = json.loads(Path("tests/fixtures/ask/source.json").read_text())
    report = evaluate(source, variant)
    assert report["passed"]
    assert len(report["checks"]) > 200


def test_existing_frozen_duplicate_grain_is_rejected():
    import json
    from pathlib import Path

    from scripts.ask_eval.serving import evaluate

    with pytest.raises(SemanticError, match="Duplicate"):
        evaluate(
            json.loads(Path("tests/fixtures/ask/source.json").read_text()), "duplicate"
        )


def test_candidate_types_and_checksum_are_validated_not_only_python_equality(
    publication,
):
    source, original, manifest, snapshot = publication
    rows = list(original)
    rows[0] = replace(rows[0], player_id=True)
    with pytest.raises(SemanticError):
        check(source, rows, manifest, snapshot)
    rows = list(original)
    rows[0] = replace(rows[0], components={key: True for key in rows[0].components})
    with pytest.raises(SemanticError):
        check(source, rows, manifest, snapshot)


def test_changed_source_cannot_reuse_trusted_snapshot_id(publication):
    source, rows, manifest, snapshot = publication
    changed = replace(source, rows=[r for r in source.rows if r["player_id"] != 2])
    rebuilt = candidate_summaries(changed, "pub-a", ["pts", "fg_pct"])
    rebuilt_manifest = candidate_manifest(
        changed,
        rebuilt,
        publication_id="pub-a",
        metrics=["pts", "fg_pct"],
        built_at=manifest["built_at"],
        expires_at=manifest["expires_at"],
        relation_version=manifest["relation_version"],
        source_timestamp=snapshot["source_timestamp"],
    )
    with pytest.raises(SemanticError):
        check(changed, rebuilt, rebuilt_manifest, snapshot)
    tampered = deepcopy(snapshot)
    tampered["rows"] = list(changed.rows)
    with pytest.raises(SemanticError):
        check(changed, rows, manifest, tampered)


def test_explicit_completeness_and_bounded_source_freshness(publication):
    from app.agent.semantic_source import snapshot_digest

    source, rows, manifest, snapshot = publication
    implicit = deepcopy(snapshot)
    implicit.pop("cohort_complete")
    implicit["sha256"] = snapshot_digest(implicit)
    with pytest.raises(SemanticError):
        check(snapshot_evidence(implicit), rows, manifest, implicit)
    for field, value in [
        ("expires_at", "2030-01-01T00:00:00+00:00"),
        ("built_at", "2026-10-21T23:58:00+00:00"),
        ("source_timestamp", "2026-10-21T23:59:59+00:00"),
    ]:
        with pytest.raises(SemanticError):
            check(source, rows, manifest | {field: value}, snapshot)


def test_metric_aliases_cannot_duplicate_publication_rows(publication):
    source, _, _, _ = publication
    with pytest.raises(SemanticError, match="Canonical"):
        candidate_summaries(source, "pub-a", ["fantasy"])


def test_frozen_parity_handles_player_without_playoffs_and_traded_player():
    import json
    from pathlib import Path

    from scripts.ask_eval.serving import evaluate

    source = json.loads(Path("tests/fixtures/ask/source.json").read_text())
    player = source["rows"][0]["player_id"]
    source["rows"] = [
        r
        for r in source["rows"]
        if not (r["player_id"] == player and r["season_type"] == "Playoffs")
    ]
    source["rows"][0]["team_abbr"] = "NEW"
    report = evaluate(source)
    assert report["passed"]
    assert any(c.get("kind") == "empty_membership_refusal" for c in report["checks"])
    assert report["endpoint_payload_parity_established"] is False
    for scope in report["manifest"]["scopes"]:
        assert scope["player_count"] == len(set(scope["player_ids"]))


def test_single_player_pilot_requires_cohort_and_empty_status_parity(publication):
    from app.agent.semantics import Query, run_query

    source, _, _, _ = publication
    query = Query(metric="pts", season="2024-25", aggregation="average", player_id=1)
    payload = run_query(source, query)
    assert "cohort" in payload
    assert {"rank", "percentile", "eligible", "exclusion_reasons", "status"} <= payload[
        "rows"
    ][0].keys()
    assert (
        run_query(source, replace(query, player_id=999))["status"] == "no_observations"
    )
