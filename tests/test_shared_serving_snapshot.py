"""Literal synthetic conformance; no retained source or cohort certification."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from app.serving import FactReadStatus, OfflineReadPolicy, ServingError, SnapshotHandle
from app.serving.contract import (
    DEPENDENCIES,
    METRIC_SEMANTICS_ID,
    canonical_json,
    content_digest,
    pts_semantics,
)

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)


def row(game, pts, **extra):
    return {
        "season": "2025-26",
        "phase": "Regular Season",
        "game_id": game,
        "player_id": 1,
        "team_id": "team-a",
        "game_date": "2025-11-01",
        "played": True,
        "pts": pts,
        "minutes": 0,
        **extra,
    }


def write_artifact(root, rows=None, transform=None):
    rows = rows if rows is not None else [row("g1", 0), row("g2", 10), row("g3", None)]
    blob = canonical_json(rows)
    root.mkdir(parents=True, exist_ok=True)
    (root / "facts").mkdir(exist_ok=True)
    (root / "facts/000000.json").write_bytes(blob)
    manifest = {
        "schema": "nba_shared_serving_snapshot/1",
        "projection_version": "affirmative-played-pts/2",
        "metric_semantics": pts_semantics(),
        "metric_semantics_id": METRIC_SEMANTICS_ID,
        "scope": {
            "season": "2025-26",
            "phase": "Regular Season",
            "start_date": "2025-11-01",
            "end_date": "2025-11-01",
            "as_of": "2025-11-01T23:59:59+00:00",
            "cutoff": "2026-10-30T00:00:00+00:00",
        },
        "lineage": {key: content_digest(key) for key in DEPENDENCIES},
        "clocks": {
            "source_captured_at": "2026-10-29T00:00:00+00:00",
            "discovered_at": "2026-10-29T00:00:00+00:00",
            "revalidated_at": "2026-10-30T00:00:00+00:00",
            "reviewed_at": "2026-10-30T00:00:00+00:00",
            "hard_expiry": None,
        },
        "freshness_policy_id": "historical-integrity/1",
        "capabilities": {
            "metric_id": "pts",
            "aggregations": ["total", "average"],
            "observed_sample": True,
            "cohort_complete": False,
            "runtime_eligible": False,
        },
        "shards": [
            {
                "path": "facts/000000.json",
                "sha256": hashlib.sha256(blob).hexdigest(),
                "byte_count": len(blob),
                "row_count": len(rows),
                "min_date": "2025-11-01",
                "max_date": "2025-11-01",
            }
        ],
        "row_count": len(rows),
    }
    manifest["dependency_ids"] = sorted(manifest["lineage"].values())
    if transform:
        transform(manifest)
    manifest["snapshot_id"] = content_digest(manifest)
    (root / "manifest.json").write_bytes(canonical_json(manifest))
    return manifest


@pytest.fixture
def artifact(tmp_path):
    # /var is a macOS symlink. Use the physical temporary root deliberately.
    root = tmp_path.resolve() / "snapshot"
    manifest = write_artifact(root)
    status = FactReadStatus(
        content_digest("status"),
        NOW,
        NOW + timedelta(hours=1),
        frozenset({manifest["snapshot_id"]}),
        {value: "active" for value in manifest["dependency_ids"]},
    )
    live = {"status": status, "now": NOW}
    policy = OfflineReadPolicy(lambda: live["status"], lambda: live["now"])
    return root, manifest, live, policy


def assert_denied(fn, code=None):
    with pytest.raises(ServingError) as error:
        fn()
    if code:
        assert error.value.code == code


def test_literal_observed_arithmetic_and_copy_isolation(artifact):
    root, manifest, live, policy = artifact
    handle = SnapshotHandle.open(root, policy)
    for aggregation, value, denominator in [("total", 10, None), ("average", 5, 2)]:
        report = handle.diagnostic(1, aggregation)
        assert (report["value"], report["numerator"], report["denominator"]) == (
            value,
            10,
            denominator,
        )
        assert (
            report["observed_games"],
            report["valid_games"],
            report["missing_component_games"],
        ) == (3, 2, 1)
        assert report["runtime_eligible"] is report["cohort_complete"] is False
        assert report["status_id"] == live["status"].status_id
    handle.read()[0]["pts"] = 999
    handle.describe()["lineage"].clear()
    assert handle.read()[0]["pts"] == 0
    assert handle.describe() == manifest
    assert handle.capabilities()["scope"] == manifest["scope"]


@pytest.mark.parametrize("points", [None, 0])
def test_missing_or_zero_known_played_minutes(artifact, points):
    root, _, live, policy = artifact
    manifest = write_artifact(root, [row("g1", points)])
    live["status"] = replace(
        live["status"], allowed_snapshot_ids=frozenset({manifest["snapshot_id"]})
    )
    result = SnapshotHandle.open(root, policy).diagnostic(1, "average")
    assert result["observed_games"] == 1
    assert result["valid_games"] == (points is not None)
    assert result["value"] == points
    if points is None:
        assert result["numerator"] is result["denominator"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"pts": True},
        {"pts": -1},
        {"pts": 0.5},
        {"pts": "10"},
        {"pts": 1000001},
        {"played": False},
        {"played": None},
        {"minutes": True},
        {"player_id": "1"},
        {"player_id": True},
        {"game_date": "2025-11-02"},
    ],
)
def test_invalid_normalized_facts_refused(artifact, changes):
    root, _, live, policy = artifact
    manifest = write_artifact(
        root,
        [row("g1", 0, **changes)]
        if "pts" not in changes
        else [{**row("g1", 0), **changes}],
    )
    live["status"] = replace(
        live["status"], allowed_snapshot_ids=frozenset({manifest["snapshot_id"]})
    )
    assert_denied(lambda: SnapshotHandle.open(root, policy))


@pytest.mark.parametrize(
    "rows", [[row("g1", 0), row("g1", 10)], [row("g2", 10), row("g1", 0)]]
)
def test_duplicate_alias_and_unordered_rows_refused(artifact, rows):
    root, _, live, policy = artifact
    manifest = write_artifact(root, rows)
    live["status"] = replace(
        live["status"], allowed_snapshot_ids=frozenset({manifest["snapshot_id"]})
    )
    assert_denied(
        lambda: SnapshotHandle.open(root, policy), "duplicate_or_unordered_row"
    )


@pytest.mark.parametrize(
    "operation", ["read", "describe", "capabilities", "diagnostic"]
)
@pytest.mark.parametrize(
    "change", ["allowlist", "identity", "capability", "unknown", "missing", "expired"]
)
def test_current_authority_revokes_previously_opened_handle(
    artifact, operation, change
):
    root, manifest, live, policy = artifact
    handle = SnapshotHandle.open(root, policy)
    handle.read()
    status = live["status"]
    states = dict(status.dependency_states)
    if change == "allowlist":
        status = replace(status, allowed_snapshot_ids=frozenset())
    elif change == "expired":
        status = replace(status, valid_until=NOW)
    else:
        dep = manifest["lineage"][
            "final_capability_id" if change == "capability" else "identity_id"
        ]
        if change == "missing":
            del states[dep]
        else:
            states[dep] = "unknown" if change == "unknown" else "revoked"
        status = replace(status, dependency_states=states)
    live["status"] = status
    fn = (
        (lambda: handle.diagnostic(1, "average"))
        if operation == "diagnostic"
        else getattr(handle, operation)
    )
    assert_denied(fn)
    assert (root / "manifest.json").read_bytes() == canonical_json(manifest)


def test_expiry_uses_access_clock_not_historical_as_of(artifact):
    root, _, live, policy = artifact
    manifest = write_artifact(
        root,
        transform=lambda m: m["clocks"].update(
            hard_expiry=(NOW + timedelta(minutes=5)).isoformat()
        ),
    )
    live["status"] = replace(
        live["status"], allowed_snapshot_ids=frozenset({manifest["snapshot_id"]})
    )
    handle = SnapshotHandle.open(root, policy)
    assert handle.diagnostic(1, "average")["scope"]["as_of"].startswith("2025")
    live["now"] = NOW + timedelta(minutes=5)
    assert_denied(handle.read, "expired_artifact")


@pytest.mark.parametrize(
    "change",
    [
        "schema",
        "projection",
        "semantics",
        "count",
        "bound",
        "path",
        "cohort",
        "runtime",
        "extra",
    ],
)
def test_manifest_cross_field_tamper_refused_even_when_rehashed(artifact, change):
    root, _, live, policy = artifact

    def mutate(m):
        if change == "schema":
            m["schema"] = "unknown/1"
        if change == "projection":
            m["projection_version"] = "unknown/1"
        if change == "semantics":
            m["metric_semantics"]["null_policy_id"] = "missing_is_zero/1"
        if change == "count":
            m["row_count"] = 2
        if change == "bound":
            m["shards"][0]["max_date"] = "2025-11-02"
        if change == "path":
            m["shards"][0]["path"] = "../facts/000000.json"
        if change == "cohort":
            m["capabilities"]["cohort_complete"] = True
        if change == "runtime":
            m["capabilities"]["runtime_eligible"] = True
        if change == "extra":
            m["attempt_id"] = "not-content"

    manifest = write_artifact(root, transform=mutate)
    live["status"] = replace(
        live["status"], allowed_snapshot_ids=frozenset({manifest["snapshot_id"]})
    )
    assert_denied(lambda: SnapshotHandle.open(root, policy))


@pytest.mark.parametrize("target", ["manifest", "shard"])
def test_changed_bytes_after_open_refused(artifact, target):
    root, _, _, policy = artifact
    handle = SnapshotHandle.open(root, policy)
    path = root / ("manifest.json" if target == "manifest" else "facts/000000.json")
    path.write_bytes(path.read_bytes() + b" ")
    assert_denied(handle.read)


@pytest.mark.parametrize("target", ["root", "facts", "shard"])
def test_symlink_paths_refused(artifact, target, tmp_path):
    root, _, _, policy = artifact
    if target == "root":
        link = tmp_path.resolve() / "link"
        link.symlink_to(root, target_is_directory=True)
        root = link
    else:
        path = root / ("facts" if target == "facts" else "facts/000000.json")
        saved = path.with_name(path.name + "-saved")
        path.rename(saved)
        path.symlink_to(saved, target_is_directory=target == "facts")
    assert_denied(lambda: SnapshotHandle.open(root, policy))


@pytest.mark.parametrize(
    "policy_change",
    [
        {"max_shard_bytes": 10},
        {"max_rows": 1},
        {"max_total_bytes": 10},
        {"max_manifest_bytes": 10},
    ],
)
def test_trusted_budgets_bound_reads(artifact, policy_change):
    root, _, _, policy = artifact
    assert_denied(lambda: SnapshotHandle.open(root, replace(policy, **policy_change)))


@pytest.mark.parametrize(
    "kwargs",
    [{"aggregation": "ratio"}, {"metric_id": "reb"}, {"scope": {"season": "2026-27"}}],
)
def test_unsupported_request_is_explicit(artifact, kwargs):
    root, _, _, policy = artifact
    handle = SnapshotHandle.open(root, policy)
    assert_denied(
        lambda: handle.diagnostic(1, **{"aggregation": "average", **kwargs}),
        "capability_not_evaluated",
    )


def test_content_identity_and_status_are_separate(artifact):
    root, m, live, policy = artifact
    original = m["snapshot_id"]
    assert write_artifact(root)["snapshot_id"] == original
    handle = SnapshotHandle.open(root, policy)
    live["status"] = replace(
        live["status"],
        status_id=content_digest("new-review"),
        dependency_states={
            key: "superseded" for key in live["status"].dependency_states
        },
    )
    result = handle.diagnostic(1, "total")
    assert result["snapshot_id"] == original
    assert result["status_id"] == content_digest("new-review")
    assert write_artifact(root, [row("g1", 1)])["snapshot_id"] != original
    assert_denied(handle.read)


def test_nullable_capture_clocks_are_honest(artifact):
    root, _, live, policy = artifact
    m = write_artifact(
        root,
        transform=lambda m: m["clocks"].update(
            source_captured_at=None, revalidated_at=None, discovered_at=None
        ),
    )
    live["status"] = replace(
        live["status"], allowed_snapshot_ids=frozenset({m["snapshot_id"]})
    )
    report = SnapshotHandle.open(root, policy).diagnostic(1, "total")
    assert "source_capture_clock_unknown" in report["limitations"]
    assert "discovery_or_revalidation_clock_unknown" in report["limitations"]
    assert "historical_integrity_only" in report["limitations"]


def test_schema_matches_literal_wire_and_semantic_definition(artifact):
    import jsonschema

    root, m, _, _ = artifact
    schema = json.loads(
        (
            Path(__file__).parents[1]
            / "contracts/shared-serving/snapshot-v1.schema.json"
        ).read_text()
    )
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )
    validator.validate(m)
    shard_validator = jsonschema.Draft202012Validator(
        {"$ref": "#/$defs/shard", "$defs": schema["$defs"]}
    )
    shard_validator.validate(json.loads((root / "facts/000000.json").read_bytes()))
    assert schema["properties"]["metric_semantics"]["const"] == pts_semantics()
    bad = copy.deepcopy(m)
    bad["capabilities"]["runtime_eligible"] = True
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(bad)


def test_nested_dependency_revocation_cannot_hide_behind_active_roots(artifact):
    root, _, live, policy = artifact
    nested = content_digest("nested-pdf")
    m = write_artifact(root, transform=lambda m: m["dependency_ids"].append(nested))
    m["dependency_ids"].sort()
    del m["snapshot_id"]
    m["snapshot_id"] = content_digest(m)
    (root / "manifest.json").write_bytes(canonical_json(m))
    status = live["status"]
    live["status"] = replace(
        status,
        allowed_snapshot_ids=frozenset({m["snapshot_id"]}),
        dependency_states={**status.dependency_states, nested: "active"},
    )
    handle = SnapshotHandle.open(root, policy)
    handle.read()
    live["status"] = replace(
        live["status"],
        dependency_states={**status.dependency_states, nested: "revoked"},
    )
    assert_denied(lambda: handle.diagnostic(1, "total"), "dependency_denied")


@pytest.mark.parametrize("mutation", ["omit", "duplicate", "unknown"])
def test_dependency_closure_shape_refused(artifact, mutation):
    root, _, live, policy = artifact

    def mutate(m):
        if mutation == "omit":
            m["dependency_ids"].pop()
        if mutation == "duplicate":
            m["dependency_ids"].append(m["dependency_ids"][-1])
        if mutation == "unknown":
            m["dependency_ids"].append("unknown")

    m = write_artifact(root, transform=mutate)
    live["status"] = replace(
        live["status"], allowed_snapshot_ids=frozenset({m["snapshot_id"]})
    )
    assert_denied(lambda: SnapshotHandle.open(root, policy))


@pytest.mark.parametrize(
    "payload", [b"NaN", b"Infinity", b"-Infinity", b'{"x":1,"x":2}']
)
def test_nonfinite_and_duplicate_json_rejects_before_output(artifact, payload):
    root, _, _, policy = artifact
    (root / "manifest.json").write_bytes(payload)
    assert_denied(lambda: SnapshotHandle.open(root, policy))


def test_old_status_cannot_extend_itself_with_distant_valid_until(artifact):
    root, _, live, policy = artifact
    live["status"] = replace(
        live["status"],
        checked_at=NOW - timedelta(days=2),
        valid_until=NOW + timedelta(days=2),
    )
    assert_denied(lambda: SnapshotHandle.open(root, policy), "expired_status")


def test_schema_numeric_budgets_match_reader_policy():
    schema = json.loads(
        (
            Path(__file__).parents[1]
            / "contracts/shared-serving/snapshot-v1.schema.json"
        ).read_text()
    )
    props = schema["properties"]
    policy = OfflineReadPolicy(lambda: None)
    assert props["row_count"]["maximum"] == policy.max_rows
    assert props["shards"]["maxItems"] == policy.max_shards
    assert (
        props["shards"]["items"]["properties"]["byte_count"]["maximum"]
        == policy.max_shard_bytes
    )
    assert schema["$defs"]["row"]["properties"]["pts"]["maximum"] == 1000000
    with pytest.raises(ServingError):
        replace(policy, max_rows=policy.max_rows + 1)


def test_permission_rechecked_after_arithmetic_before_emission(artifact, monkeypatch):
    import app.serving.snapshot as reader

    root, _, live, policy = artifact
    handle = SnapshotHandle.open(root, policy)
    canonical_aggregate = reader.aggregate

    def revoke_after_aggregate(*args):
        result = canonical_aggregate(*args)
        live["status"] = replace(live["status"], allowed_snapshot_ids=frozenset())
        return result

    monkeypatch.setattr(reader, "aggregate", revoke_after_aggregate)
    assert_denied(lambda: handle.diagnostic(1, "average"), "snapshot_not_allowed")


def test_diagnostic_records_exact_final_authorization_clock_without_extra_read(
    artifact,
):
    root, _, _, policy = artifact
    instants = [
        NOW,
        NOW,
        NOW + timedelta(seconds=1),
        NOW + timedelta(seconds=2),
        NOW + timedelta(seconds=3),
    ]
    calls = []

    def clock():
        value = instants[len(calls)]
        calls.append(value)
        return value

    handle = SnapshotHandle.open(root, replace(policy, clock=clock))
    result = handle.diagnostic(1, "average")
    assert len(calls) == 5
    assert result["freshness_checked_at"] == instants[-1].isoformat()


@pytest.mark.parametrize("scope", [1, "invalid", [], [("season", "2025-26")]])
def test_malformed_scope_arguments_have_bounded_refusal(artifact, scope):
    root, _, _, policy = artifact
    handle = SnapshotHandle.open(root, policy)
    assert_denied(
        lambda: handle.diagnostic(1, "total", scope=scope), "capability_not_evaluated"
    )


def test_broken_scope_mapping_cannot_leak_raw_exception(artifact):
    from collections.abc import Mapping

    class BrokenScope(Mapping):
        def __iter__(self):
            raise RuntimeError("raw scope details")

        def __len__(self):
            return 1

        def __getitem__(self, key):
            raise RuntimeError("raw scope details")

    root, _, _, policy = artifact
    handle = SnapshotHandle.open(root, policy)
    with pytest.raises(ServingError) as error:
        handle.diagnostic(1, "total", scope=BrokenScope())
    assert error.value.code == "capability_not_evaluated"
    assert error.value.__suppress_context__ is True
    assert "raw scope details" not in str(error.value)


@pytest.mark.parametrize("target", ["load_contract", "aggregate"])
@pytest.mark.parametrize("kind", ["io", "semantic", "invalid"])
def test_catalog_and_arithmetic_failures_have_bounded_semantic_refusal(
    artifact, monkeypatch, target, kind
):
    import app.serving.snapshot as reader
    from app.agent.semantics import SemanticError

    root, _, _, policy = artifact
    handle = SnapshotHandle.open(root, policy)

    def fail(*args):
        if kind == "io":
            raise OSError("raw catalog details")
        if kind == "semantic":
            raise SemanticError("unsupported_metric", "raw catalog details")
        raise ValueError("raw catalog details")

    monkeypatch.setattr(reader, target, fail)
    with pytest.raises(ServingError) as error:
        handle.diagnostic(1, "total")
    assert error.value.code == "semantic_mismatch"
    assert error.value.__suppress_context__ is True
    assert "raw catalog details" not in str(error.value)


@pytest.mark.parametrize("known_capture", [False, True])
def test_capture_and_review_clock_limitations_are_distinct(artifact, known_capture):
    root, _, live, policy = artifact

    def change(m):
        m["clocks"].update(discovered_at=None, revalidated_at=None)
        if not known_capture:
            m["clocks"]["source_captured_at"] = None

    m = write_artifact(root, transform=change)
    live["status"] = replace(
        live["status"], allowed_snapshot_ids=frozenset({m["snapshot_id"]})
    )
    result = SnapshotHandle.open(root, policy).diagnostic(1, "total")
    assert ("source_capture_clock_unknown" in result["limitations"]) is (
        not known_capture
    )
    assert "discovery_or_revalidation_clock_unknown" in result["limitations"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("allowed_snapshot_ids", "substring"),
        ("allowed_snapshot_ids", []),
        ("allowed_snapshot_ids", set()),
        ("allowed_snapshot_ids", frozenset({1})),
        ("dependency_states", []),
        ("dependency_states", None),
        ("dependency_states", {"not-a-digest": "active"}),
        ("dependency_states", {"a" * 64: True}),
    ],
)
def test_status_container_types_fail_closed(artifact, field, value):
    root, m, live, policy = artifact
    handle = SnapshotHandle.open(root, policy)
    if value == "substring":
        value = "prefix" + m["snapshot_id"] + "suffix"
    live["status"] = replace(live["status"], **{field: value})
    assert_denied(handle.read, "invalid_status")


def test_readonly_dependency_mapping_is_supported(artifact):
    from types import MappingProxyType

    root, _, live, policy = artifact
    live["status"] = replace(
        live["status"],
        dependency_states=MappingProxyType(dict(live["status"].dependency_states)),
    )
    assert SnapshotHandle.open(root, policy).diagnostic(1, "total")["value"] == 10


def test_earlier_requested_scope_cannot_rescue_expired_historical_artifact(artifact):
    root, _, live, policy = artifact
    m = write_artifact(
        root,
        transform=lambda m: m["clocks"].update(
            hard_expiry=(NOW + timedelta(minutes=5)).isoformat()
        ),
    )
    live["status"] = replace(
        live["status"], allowed_snapshot_ids=frozenset({m["snapshot_id"]})
    )
    handle = SnapshotHandle.open(root, policy)
    earlier = {**m["scope"], "as_of": "2025-11-01T01:00:00+00:00"}
    assert_denied(
        lambda: handle.diagnostic(1, "total", scope=earlier), "capability_not_evaluated"
    )
    live["now"] = NOW + timedelta(minutes=5)
    assert_denied(
        lambda: handle.diagnostic(1, "total", scope=earlier), "expired_artifact"
    )
    assert (root / "manifest.json").read_bytes() == canonical_json(m)


@pytest.mark.parametrize("target", ["as_of", "cutoff"])
def test_future_query_or_knowledge_clock_is_refused(artifact, target):
    root, _, live, policy = artifact

    def change(m):
        future = (NOW + timedelta(seconds=1)).isoformat()
        m["scope"]["cutoff"] = future
        if target == "as_of":
            m["scope"]["as_of"] = future

    m = write_artifact(root, transform=change)
    live["status"] = replace(
        live["status"], allowed_snapshot_ids=frozenset({m["snapshot_id"]})
    )
    assert_denied(lambda: SnapshotHandle.open(root, policy), "future_evidence_clock")


def test_schema_requires_nonempty_snapshot_row_count():
    schema = json.loads(
        (
            Path(__file__).parents[1]
            / "contracts/shared-serving/snapshot-v1.schema.json"
        ).read_text()
    )
    assert schema["properties"]["row_count"]["minimum"] == 1


def test_prior_unselected_projection_version_is_unsupported(artifact):
    root, _, live, policy = artifact
    m = write_artifact(
        root,
        transform=lambda m: m.update(projection_version="affirmative-played-pts/1"),
    )
    live["status"] = replace(
        live["status"], allowed_snapshot_ids=frozenset({m["snapshot_id"]})
    )
    assert_denied(lambda: SnapshotHandle.open(root, policy), "unsupported_version")
