"""Synthetic, offline rejection cases for the proposed app publication contract."""

import json
import os
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import SchemaError, ValidationError

ROOT = Path(__file__).resolve().parents[1]
from app.serving import publication_contract as contract  # noqa: E402


def test_publication_validator_module_command():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.serving.publication_contract",
            str(contract.CONTRACTS / "synthetic-manifest.json"),
            "--binding",
            str(contract.CONTRACTS / "synthetic-binding.json"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "Publication contract consistent" in result.stdout


def test_schema_cache_retains_payload_checks_and_isolates_error_schema(
    tmp_path, monkeypatch
):
    schema = {"type": "object", "properties": {"value": {"type": "integer"}}}
    (tmp_path / "publication-status.schema.json").write_text(json.dumps(schema))
    monkeypatch.setattr(contract, "CONTRACTS", tmp_path)
    contract._validated_schema_text.cache_clear()
    calls = []
    original = contract.Draft202012Validator.check_schema

    def check(cls, value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(
        contract.Draft202012Validator, "check_schema", classmethod(check)
    )
    contract.schema_check("status", {"value": 1})
    with pytest.raises(ValidationError) as error:
        contract.schema_check("status", {"value": "invalid"})
    assert error.value.validator == "type"
    assert list(error.value.path) == ["value"]
    error.value.schema["type"] = "string"
    with pytest.raises(ValidationError):
        contract.schema_check("status", {"value": "still invalid"})
    contract.schema_check("status", {"value": 2})
    assert len(calls) == 1


def test_schema_cache_uses_content_and_recovers_after_corrupt_or_missing_source(
    tmp_path, monkeypatch
):
    path = tmp_path / "publication-status.schema.json"
    original = json.dumps({"type": "object", "properties": {"value": {"minimum": 1}}})
    changed = original.replace('"minimum": 1', '"minimum": 9')
    assert len(original) == len(changed)
    path.write_text(original)
    info = path.stat()
    monkeypatch.setattr(contract, "CONTRACTS", tmp_path)
    contract._validated_schema_text.cache_clear()
    contract.schema_check("status", {"value": 5})
    path.write_text(changed)
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    with pytest.raises(ValidationError):
        contract.schema_check("status", {"value": 5})
    path.write_text('{"type": 123}')
    with pytest.raises(SchemaError):
        contract.schema_check("status", {"value": 5})
    path.write_text(original)
    contract.schema_check("status", {"value": 5})
    path.unlink()
    with pytest.raises(FileNotFoundError):
        contract.schema_check("status", {"value": 5})
    path.write_text(original)
    contract.schema_check("status", {"value": 5})


def test_schema_cache_parallel_calls_do_not_share_mutable_validation_state(
    tmp_path, monkeypatch
):
    schema = {"type": "object", "properties": {"value": {"type": "integer"}}}
    (tmp_path / "publication-status.schema.json").write_text(json.dumps(schema))
    monkeypatch.setattr(contract, "CONTRACTS", tmp_path)
    contract._validated_schema_text.cache_clear()

    def validate(index):
        contract.schema_check("status", {"value": index})
        with pytest.raises(ValidationError) as error:
            contract.schema_check("status", {"value": "invalid"})
        error.value.schema["type"] = "null"
        contract.schema_check("status", {"value": index})
        return error.value.schema

    with ThreadPoolExecutor(max_workers=8) as pool:
        errors = list(pool.map(validate, range(32)))
    assert len({id(schema) for schema in errors}) == 32


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Contract checks must not use the network")

    monkeypatch.setattr(socket, "socket", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def manifest():
    return json.loads((contract.CONTRACTS / "synthetic-manifest.json").read_text())


@pytest.fixture
def binding():
    return json.loads((contract.CONTRACTS / "synthetic-binding.json").read_text())


def reseal(document):
    document["allocation_key"] = contract.allocation_key(document)
    document["input_fingerprint"] = contract.fingerprint(document)
    for asset in document["assets"]:
        asset["built_from"] = document["input_fingerprint"]


def set_at(document, path, value):
    for key in path[:-1]:
        document = document[key]
    document[path[-1]] = value


def test_synthetic_examples_and_status_allowlist(manifest, binding):
    contract.validate_binding(binding, manifest)
    status = contract.public_status(manifest, manifest["validation_as_of"])
    assert status == json.loads(
        (contract.CONTRACTS / "synthetic-status.json").read_text()
    )
    assert "inputs" not in status and "relations" not in status
    assert "selected_games" not in status and "versions" not in status
    status["private_evidence_path"] = "synthetic://should-not-escape"
    with pytest.raises(ValidationError):
        contract.schema_check("status", status)


@pytest.mark.parametrize(
    "field", ["versions", "inputs", "selected_games", "checks", "scope"]
)
def test_required_identity_authority_and_scope(manifest, field):
    del manifest[field]
    with pytest.raises(ValidationError):
        contract.validate_manifest(manifest)


@pytest.mark.parametrize(
    "path,value",
    [
        (("coverage", "unresolved_identities"), 1),
        (("coverage", "open_required_holds"), 1),
        (("coverage", "expected_games"), 2),
        (("coverage", "observed_participants"), 5),
        (("coverage", "played_rows"), 3),
        (("selected_games", 0, "played"), 5),
        (("selected_games", 0, "game_id"), "date-and-teams"),
        (("selected_games", 0, "mapping_version"), "other-identity/1"),
        (("selected_games", 0, "last_verified_at"), "2026-10-31T00:00:00Z"),
        (("scope", "provider"), "nba_api"),
        (("scope", "phase"), "Playoffs"),
        (("purpose",), "production"),
        (("versions", "source_policy"), "fallback-allowed/1"),
        (("checks", 0, "status"), "failed"),
        (("checks", 0, "receipt_digest"), "missing"),
        (("assets", 0, "relation"), "synthetic-project.nba_gold.accepted_game_results"),
        (("assets", 0, "rows"), 2),
        (("assets", 1, "dependencies"), []),
        (("assets", 3, "fields"), ["pts"]),
        (("inputs", 0, "evidence_kind"), "retained"),
        (("inputs", 0, "captured_at"), "2026-11-02T00:00:00Z"),
        (("freshness", "discovery_verified_at"), "2026-11-02T00:00:00Z"),
        (("freshness", "expires_at"), "2026-12-01T00:00:00Z"),
        (("freshness", "expires_at"), "2026-11-01T16:00:00Z"),
        (("validation_as_of",), "2026-10-01T16:00:00Z"),
        (("auxiliary", 0, "status"), "fresh"),
    ],
)
def test_rejects_inconsistent_candidate_even_with_recomputed_fingerprint(
    manifest, path, value
):
    set_at(manifest, path, value)
    reseal(manifest)
    with pytest.raises((ValueError, ValidationError)):
        contract.validate_manifest(manifest)


@pytest.mark.parametrize(
    "field", ["inputs", "selected_games", "checks", "assets", "fields", "auxiliary"]
)
def test_duplicate_entries_rejected(manifest, field):
    manifest[field].append(deepcopy(manifest[field][0]))
    reseal(manifest)
    with pytest.raises((ValueError, ValidationError)):
        contract.validate_manifest(manifest)


@pytest.mark.parametrize(
    "field", ["inputs", "checks", "assets", "fields", "auxiliary", "capabilities"]
)
def test_incomplete_contract_rejected(manifest, field):
    manifest[field].pop()
    reseal(manifest)
    with pytest.raises((ValueError, ValidationError)):
        contract.validate_manifest(manifest)


def test_missing_stat_is_not_zero(manifest):
    minute = next(f for f in manifest["fields"] if f["name"] == "min")
    minute.update(status="nullable", missing_count=1)
    contract.validate_manifest(manifest)
    points = next(f for f in manifest["fields"] if f["name"] == "pts")
    points.update(status="nullable", missing_count=1)
    with pytest.raises(ValueError, match="availability"):
        contract.validate_manifest(manifest)


def test_input_and_asset_fingerprint_tampering(manifest):
    manifest["versions"]["producer_code"] = "changed/2"
    with pytest.raises(ValueError, match="allocation key|fingerprint"):
        contract.validate_manifest(manifest)
    reseal(manifest)
    manifest["assets"][0]["built_from"] = "0" * 64
    with pytest.raises(ValueError, match="fingerprint"):
        contract.validate_manifest(manifest)


@pytest.mark.parametrize(
    "path,value",
    [
        (("manifest_digest",), "0" * 64),
        (("publication_id",), "pub_0000000000000002"),
        (("relations", "dim_game"), "synthetic-project.other.dim_game"),
        (("expires_at",), "2026-12-01T00:00:00Z"),
        (("routes", 0, "mode"), "release"),
        (("routes", 6, "target"), "pub_0000000000000002"),
    ],
)
def test_binding_cannot_mix_releases_or_scopes(manifest, binding, path, value):
    set_at(binding, path, value)
    with pytest.raises((ValueError, ValidationError)):
        contract.validate_binding(binding, manifest)


def test_binding_requires_whole_scope_map(manifest, binding):
    binding["routes"].pop()
    with pytest.raises(ValueError, match="scope routing"):
        contract.validate_binding(binding, manifest)


def test_cutoff_expiry_is_elapsed_seconds_across_fall_dst(manifest):
    # Oct 31 ends Nov 1 04:00 UTC; DST ends later that morning.
    # Ten elapsed days must not become ten local days plus an extra hour.
    manifest["freshness"]["source_valid_until"] = "2026-11-12T00:00:00Z"
    manifest["freshness"]["expires_at"] = "2026-11-11T04:00:01Z"
    with pytest.raises(ValueError, match="freshness"):
        contract.validate_manifest(manifest)


@pytest.mark.parametrize(
    "name",
    [
        "core_performance",
        "dim_player",
        "dim_team",
        "core_player_summary",
        "core_player_search",
        "core_player_windows",
        "core_player_trends",
    ],
)
def test_core_models_cannot_claim_empty_rows(manifest, name):
    next(a for a in manifest["assets"] if a["name"] == name)["rows"] = 0
    with pytest.raises(ValueError, match="cardinality"):
        contract.validate_manifest(manifest)


def test_materialization_clock_is_bound_but_attempt_completion_is_not(manifest):
    original_key = contract.allocation_key(manifest)
    original_fingerprint = contract.fingerprint(manifest)
    manifest["built_at"] = "2026-11-02T00:00:00Z"
    assert contract.fingerprint(manifest) == original_fingerprint
    manifest["materialized_at"] = "2026-11-01T17:00:00Z"
    assert contract.allocation_key(manifest) == original_key
    assert contract.fingerprint(manifest) != original_fingerprint
    with pytest.raises(ValueError, match="fingerprint"):
        contract.validate_manifest(manifest)


def metric_records(model="core_player_summary", observed=2):
    rows = []
    for metric, spec in contract.PROFILE["metrics"].items():
        for aggregation in spec["aggregations"]:
            if model == "core_performance" and aggregation == "per_game":
                continue
            numerator = observed if metric == "gp" else 3
            denominator = None if aggregation == "total" else observed
            rows.append(
                dict(
                    metric=metric,
                    aggregation=aggregation,
                    value=numerator if denominator is None else numerator / denominator,
                    unit=spec["unit"]
                    + ("_per_game" if aggregation == "per_game" else ""),
                    observed_games=observed,
                    valid_games=observed,
                    missing_games=0,
                    numerator=numerator,
                    denominator=denominator,
                    precision="whole_minute_reported"
                    if metric == "min"
                    else "minute_derived"
                    if aggregation == "per36"
                    else "ratio_derived"
                    if aggregation == "ratio"
                    else "exact_count",
                    unavailable_reason=None,
                )
            )
    return rows


@pytest.mark.parametrize(
    "model,observed",
    [("core_player_summary", 2), ("core_player_windows", 2), ("core_performance", 1)],
)
def test_explicit_metric_sets(model, observed):
    contract.validate_metric_records(model, metric_records(model, observed), observed)


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate",
        "missing",
        "wrong_unit",
        "wrong_value",
        "wrong_denominator",
        "zero_as_zero",
        "unknown_metric",
        "unknown_aggregation",
    ],
)
def test_rejects_ambiguous_or_inconsistent_metric_records(kind):
    rows = metric_records()
    if kind == "duplicate":
        rows.append(deepcopy(rows[0]))
    elif kind == "missing":
        rows.pop()
    elif kind == "wrong_unit":
        rows[0]["unit"] = "ratio"
    elif kind == "wrong_value":
        rows[0]["value"] = 99
    elif kind == "wrong_denominator":
        next(r for r in rows if r["aggregation"] == "per_game")["denominator"] = 5
    elif kind == "zero_as_zero":
        row = next(r for r in rows if r["aggregation"] == "ratio")
        row.update(denominator=0, value=0)
    elif kind == "unknown_metric":
        rows[0]["metric"] = "invented"
    elif kind == "unknown_aggregation":
        rows[0]["aggregation"] = "average_of_ratios"
    with pytest.raises((ValueError, ValidationError)):
        contract.validate_metric_records("core_player_summary", rows, 2)


def test_metric_nulls_and_zero_denominators_are_explicit():
    rows = metric_records()
    row = next(r for r in rows if r["metric"] == "fg_pct")
    row.update(
        numerator=0, denominator=0, value=None, unavailable_reason="zero_denominator"
    )
    contract.validate_metric_records("core_player_summary", rows, 2)
    for row in rows:
        row.update(
            value=None,
            numerator=None,
            denominator=None,
            observed_games=0,
            valid_games=0,
            missing_games=0,
            unavailable_reason="no_appearances",
        )
        if row["metric"] == "gp":
            row.update(value=0, numerator=0, unavailable_reason=None)
    contract.validate_metric_records("core_player_windows", rows, 0)


def test_freshness_boundaries_use_source_clock(manifest):
    # Enlarge the earlier producer validity ceiling solely for this synthetic test.
    manifest["freshness"]["source_valid_until"] = "2026-11-11T04:00:00Z"
    manifest["freshness"]["expires_at"] = "2026-11-11T04:00:00Z"
    assert (
        contract.public_status(manifest, "2026-11-09T12:00:00Z")["freshness"] == "fresh"
    )
    assert (
        contract.public_status(manifest, "2026-11-09T12:00:01Z")["freshness"]
        == "stale_usable"
    )
    assert (
        contract.public_status(manifest, "2026-11-11T04:00:00Z")["freshness"]
        == "expired"
    )
    manifest["built_at"] = "2026-11-12T00:00:00Z"
    assert (
        contract.public_status(manifest, "2026-11-12T00:00:00Z")["freshness"]
        == "expired"
    )


def test_retained_replay_clock_is_pinned_per_run_not_example(manifest):
    earlier_key = contract.allocation_key(manifest)
    manifest["validation_as_of"] = "2026-11-02T05:00:00Z"
    manifest["materialized_at"] = manifest["built_at"] = "2026-11-02T05:00:00Z"
    manifest["allocation_key"] = contract.allocation_key(manifest)
    manifest["input_fingerprint"] = contract.fingerprint(manifest)
    for asset in manifest["assets"]:
        asset["built_from"] = manifest["input_fingerprint"]
    assert manifest["allocation_key"] != earlier_key
    contract.validate_manifest(manifest)
    # Changing the run clock without rebinding the candidate is rejected.
    manifest["validation_as_of"] = "2026-11-02T06:00:00Z"
    with pytest.raises(ValueError, match="allocation key differs"):
        contract.validate_manifest(manifest)


def test_metric_schema_cache_reuses_only_exact_typed_valid_records():
    from jsonschema import Draft202012Validator

    class CountingValidator:
        def __init__(self):
            self.inner = Draft202012Validator(
                {"type": "object", "properties": {"n": {"type": "integer"}}}
            )
            self.calls = 0

        def validate(self, value):
            self.calls += 1
            self.inner.validate(value)

    validator = CountingValidator()
    with contract.metric_schema_validation_cache():
        contract._validate_metric_schema(validator, b"schema-1", {"n": 1})
        contract._validate_metric_schema(validator, b"schema-1", {"n": 1})
        assert validator.calls == 1
        with pytest.raises(ValidationError):
            contract._validate_metric_schema(validator, b"schema-1", {"n": True})
        contract._validate_metric_schema(validator, b"schema-2", {"n": 1})
        assert validator.calls == 3
    contract._validate_metric_schema(validator, b"schema-1", {"n": 1})
    assert validator.calls == 4
