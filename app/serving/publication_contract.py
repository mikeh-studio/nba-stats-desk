"""Offline contract checks, not a publisher or an authority verifier.

Receipts, relation contents and signatures must be verified by trusted producer/
reader composition before use. No cloud clients or runtime source selection here.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator, FormatChecker

from app.serving.weekly_profile import profile_for, weekly_scope

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts" / "publication"
PROFILE = json.loads((CONTRACTS / "core-stats-v1.json").read_text())
_METRIC_SCHEMA_CACHE = ContextVar("metric_schema_validation_cache", default=None)


@contextmanager
def metric_schema_validation_cache():
    """Reuse schema results only for identical records within one model build."""
    token = _METRIC_SCHEMA_CACHE.set(OrderedDict())
    try:
        yield
    finally:
        _METRIC_SCHEMA_CACHE.reset(token)


def _validate_metric_schema(validator, schema_digest, record):
    cache = _METRIC_SCHEMA_CACHE.get()
    if cache is None or not isinstance(record, dict):
        validator.validate(record)
        return
    # Include the value types: Python considers True equal to 1, whereas the
    # JSON schema does not. Unhashable inputs take the normal validation path.
    try:
        key = (schema_digest, tuple(sorted((k, type(v), v) for k, v in record.items())))
        hash(key)
    except (TypeError, ValueError):
        validator.validate(record)
        return
    if key in cache:
        cache.move_to_end(key)
        return
    validator.validate(record)
    cache[key] = None
    if len(cache) > 32768:
        cache.popitem(last=False)


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def allocation_key(manifest):
    return digest(
        {
            "profile_digest": digest(profile_for(manifest)),
            **{
                key: manifest[key]
                for key in (
                    "purpose",
                    "evidence_kind",
                    "versions",
                    "scope",
                    "inputs",
                    "selected_games",
                    "validation_as_of",
                )
            },
        }
    )


def fingerprint(manifest):
    return digest(
        {
            "allocation_key": allocation_key(manifest),
            "materialized_at": manifest["materialized_at"],
        }
    )


def require(condition, message):
    if not condition:
        raise ValueError(message)


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def schema_check(kind, value):
    suffix = "-v2" if value.get("version") == f"publication-{kind}/2" else ""
    schema = json.loads(
        (CONTRACTS / f"publication-{kind}{suffix}.schema.json").read_text()
    )
    if suffix and kind in {"manifest", "status"}:
        scope = weekly_scope(value["scope"])
        require(
            value["data_through"] == scope["last"], "cutoff differs from rolling scope"
        )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)


def unique_index(rows, key):
    indexed = {row[key]: row for row in rows}
    require(len(indexed) == len(rows), f"duplicate {key}")
    return indexed


def validate_manifest(manifest):
    """Reject inconsistent declarations; this does not authenticate evidence."""
    profile = profile_for(manifest)
    schema_check("manifest", manifest)
    if manifest["version"] == "publication-manifest/2":
        source_policy = (
            "nba_manual_selection/1"
            if manifest["scope"]["provider"] == "nba_api"
            else "unified_source_selection/1"
        )
        require(
            manifest["versions"]["source_policy"] == source_policy,
            "source policy/provider differs",
        )
    require(
        manifest["allocation_key"] == allocation_key(manifest), "allocation key differs"
    )
    require(
        manifest["input_fingerprint"] == fingerprint(manifest),
        "input fingerprint differs",
    )
    require(
        set(manifest["capabilities"]) == set(profile["capabilities"]),
        "capability set differs",
    )
    inputs = unique_index(manifest["inputs"], "role")
    require(
        set(inputs) == set(profile["required_inputs"]),
        "required authority/input missing",
    )
    require(
        all(i["evidence_kind"] == manifest["evidence_kind"] for i in inputs.values()),
        "mixed evidence classes",
    )
    require(
        manifest["purpose"] == "offline_replay"
        or manifest["evidence_kind"] == "retained",
        "synthetic evidence cannot be activated",
    )
    checks = unique_index(manifest["checks"], "name")
    require(set(checks) == set(profile["required_checks"]), "required check missing")
    games = unique_index(manifest["selected_games"], "game_uid")
    unique_index(manifest["selected_games"], "game_id")
    require(
        all(
            g["mapping_version"] == manifest["versions"]["identity"]
            for g in games.values()
        ),
        "identity version differs",
    )
    coverage = manifest["coverage"]
    require(
        coverage["expected_games"] == coverage["observed_games"] == len(games),
        "game coverage differs",
    )
    require(
        coverage["expected_participants"]
        == coverage["observed_participants"]
        == sum(g["participants"] for g in games.values()),
        "participant coverage differs",
    )
    require(
        coverage["played_rows"] == sum(g["played"] for g in games.values()),
        "played row coverage differs",
    )
    require(
        all(0 < g["played"] <= g["participants"] for g in games.values()),
        "played/participant counts invalid",
    )
    fields = unique_index(manifest["fields"], "name")
    require(
        set(fields)
        == set(profile["assets"]["fct_player_game_stats"]["required_fields"]),
        "player field coverage differs",
    )
    nullable = {"min", "fg_pct", "ft_pct"}
    for name, field in fields.items():
        missing = field["missing_count"]
        require(missing <= coverage["played_rows"], "missing count exceeds played rows")
        require(
            (missing == 0 and field["status"] == "complete")
            or (name in nullable and missing > 0 and field["status"] == "nullable"),
            "invalid field availability",
        )
    assets = unique_index(manifest["assets"], "name")
    require(set(assets) == set(profile["assets"]), "required asset set differs")
    unique_index(manifest["assets"], "relation")
    datasets = {a["relation"].rsplit(".", 1)[0] for a in assets.values()}
    require(len(datasets) == 1, "release spans mutable or mixed datasets")
    expected_dataset = "release_" + manifest["publication_id"][4:]
    require(
        (
            next(iter(datasets)).split(".")[1] == expected_dataset
            if manifest["version"] == "publication-manifest/1"
            else next(iter(datasets)).split(".")[1].startswith("nba_core_")
            and next(iter(datasets)).split(".")[1].endswith("_" + expected_dataset)
        ),
        "dataset does not bind publication ID",
    )
    for name, asset in assets.items():
        spec = profile["assets"][name]
        require(asset["relation"].rsplit(".", 1)[1] == name, "relation alias differs")
        require(
            asset["built_from"] == manifest["input_fingerprint"],
            "mixed input fingerprints",
        )
        require(
            asset["dependencies"] == spec["dependencies"], "dependency closure differs"
        )
        require(
            asset["fields"] == spec["required_fields"], "asset field schema differs"
        )
    for name, count in {
        "accepted_game_results": len(games),
        "dim_game": len(games),
        "accepted_player_appearances": coverage["played_rows"],
        "fct_player_game_stats": coverage["played_rows"],
        "participant_manifest": coverage["observed_participants"],
        "fct_team_game_results": 2 * len(games),
        "core_performance": coverage["played_rows"],
        "dim_player": coverage["observed_players"],
        "core_player_summary": coverage["observed_players"],
        "core_player_search": coverage["observed_players"],
        "core_player_windows": len(profile["windows"]) * coverage["observed_players"],
        "core_player_trends": len(profile["trend_metrics"])
        * coverage["observed_players"],
        "dim_team": coverage["observed_teams"],
    }.items():
        require(assets[name]["rows"] == count, "fact/participation cardinality differs")
    require(
        coverage["observed_players"] <= coverage["played_rows"],
        "distinct player count exceeds appearances",
    )
    require(
        2 <= coverage["observed_teams"] <= 2 * len(games), "distinct team count invalid"
    )
    auxiliary = unique_index(manifest["auxiliary"], "domain")
    require(
        set(auxiliary)
        == {
            "schedule",
            "injury",
            "positions",
            "shots",
            "similarity",
            "gamebook_pdf",
            "pbp",
            "research_context",
        },
        "auxiliary disposition missing",
    )
    as_of = timestamp(manifest["validation_as_of"])
    built = timestamp(manifest["built_at"])
    require(
        as_of <= timestamp(manifest["materialized_at"]) <= built,
        "candidate materialization clock invalid",
    )
    # Replay time belongs to the pinned run, not the reusable metric profile.
    # allocation_key() binds it; original receipt clocks and expiry below still
    # constrain it. The example clock is only for the synthetic fixture.
    for item in inputs.values():
        require(timestamp(item["captured_at"]) <= as_of, "future input capture")
    for game in games.values():
        require(
            timestamp(game["captured_at"])
            <= timestamp(game["last_verified_at"])
            <= as_of,
            "invalid source clocks",
        )
    fresh = manifest["freshness"]
    discovery = timestamp(fresh["discovery_verified_at"])
    require(discovery <= as_of, "future discovery")
    require(
        discovery == timestamp(inputs["discovery"]["captured_at"]),
        "discovery clock differs from receipt",
    )
    expiry = timestamp(fresh["expires_at"])
    # The producer's earlier source/milestone expiry always wins.
    cutoff_end = datetime.combine(
        date.fromisoformat(manifest["data_through"]) + timedelta(days=1),
        time(),
        ZoneInfo("America/New_York"),
    ).astimezone(timezone.utc)
    ceiling = min(
        discovery + timedelta(seconds=profile["freshness"]["expiry_seconds"]),
        cutoff_end + timedelta(seconds=profile["freshness"]["expiry_seconds"]),
        timestamp(fresh["source_valid_until"]),
    )
    require(as_of < expiry <= ceiling, "invalid/expired freshness boundary")


def validate_binding(binding, manifest):
    validate_manifest(manifest)
    schema_check("binding", binding)
    require(
        binding["version"].rsplit("/", 1)[1] == manifest["version"].rsplit("/", 1)[1]
        and binding["profile"] == manifest["profile"],
        "binding profile/version differs",
    )
    require(
        binding["publication_id"] == manifest["publication_id"]
        and binding["manifest_digest"] == digest(manifest),
        "binding manifest differs",
    )
    require(
        binding["relations"] == {a["name"]: a["relation"] for a in manifest["assets"]},
        "binding relations differ",
    )
    require(
        binding["capabilities"] == manifest["capabilities"]
        and binding["expires_at"] == manifest["freshness"]["expires_at"],
        "binding capability/expiry differs",
    )
    routes = {(r["season"], r["phase"]): r for r in binding["routes"]}
    require(len(routes) == len(binding["routes"]), "duplicate scope route")
    expected = {
        (f"{y}-{str(y + 1)[-2:]}", phase)
        for y in range(2023, int(manifest["scope"]["season"][:4]) + 1)
        for phase in ("Regular Season", "Playoffs")
    }
    require(set(routes) == expected, "complete scope routing map required")
    for scope, route in routes.items():
        if scope == (manifest["scope"]["season"], manifest["scope"]["phase"]):
            require(
                route["mode"] == "release"
                and route["target"] == manifest["publication_id"],
                "current scope must bind release",
            )
        else:
            require(
                route["mode"] in ("legacy", "unsupported"),
                "uncovered scope cannot claim unified publication",
            )


def validate_metric_records(
    model, records, observed_games, *, minute_precision="whole_minute_reported"
):
    """Validate core nested records; raw-to-metric arithmetic needs independent tests."""
    require(
        model in {"core_player_summary", "core_player_windows", "core_performance"},
        "unknown metric model",
    )
    require(
        type(observed_games) is int and observed_games >= 0, "invalid observed count"
    )
    if model == "core_performance":
        require(observed_games == 1, "performance must be one appearance")
    schema_text = (CONTRACTS / "metric-record.schema.json").read_text()
    schema = json.loads(schema_text)
    schema_digest = hashlib.sha256(schema_text.encode()).digest()
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    expected = {
        (metric, aggregation)
        for metric, spec in PROFILE["metrics"].items()
        for aggregation in spec["aggregations"]
        if model != "core_performance" or aggregation != "per_game"
    }
    keys = {(r.get("metric"), r.get("aggregation")) for r in records}
    require(len(keys) == len(records) and keys == expected, "metric slots differ")
    for record in records:
        _validate_metric_schema(validator, schema_digest, record)
        metric, aggregation = record["metric"], record["aggregation"]
        unit = PROFILE["metrics"][metric]["unit"]
        if aggregation == "per_game":
            unit += "_per_game"
        require(record["unit"] == unit, "metric unit differs")
        valid, missing = record["valid_games"], record["missing_games"]
        require(
            record["observed_games"] == observed_games == valid + missing,
            "metric denominator counts differ",
        )
        require(
            metric in {"min", "pts_per36", "ast_per36"} or missing == 0,
            "required count components cannot be missing",
        )
        precision = (
            minute_precision
            if metric == "min"
            else "minute_derived"
            if aggregation == "per36"
            else "ratio_derived"
            if aggregation == "ratio"
            else "exact_count"
        )
        require(record["precision"] == precision, "metric precision differs")
        value, numerator, denominator = (
            record[k] for k in ("value", "numerator", "denominator")
        )
        require(
            all(v is None or math.isfinite(v) for v in (value, numerator, denominator)),
            "nonfinite metric",
        )
        reason = record["unavailable_reason"]
        if metric == "gp":
            require(
                value == numerator == observed_games
                and denominator is None
                and missing == 0
                and reason is None,
                "games played differs",
            )
            continue
        if valid == 0:
            require(
                value is None
                and numerator is None
                and denominator is None
                and reason
                == ("no_appearances" if observed_games == 0 else "missing_components"),
                "empty metric must be unavailable",
            )
            continue
        require(numerator is not None, "metric numerator missing")
        require(metric == "plus_minus" or numerator >= 0, "negative metric numerator")
        require(denominator is None or denominator >= 0, "negative denominator")
        if aggregation == "total":
            require(denominator is None, "total denominator must be null")
            expected_value = numerator
        else:
            require(denominator is not None, "metric denominator missing")
            if aggregation == "per_game":
                require(denominator == valid, "per-game denominator differs")
            if denominator == 0:
                require(
                    value is None and reason == "zero_denominator",
                    "zero denominator must be unavailable",
                )
                continue
            expected_value = numerator / denominator
        require(
            value is not None
            and reason is None
            and math.isclose(value, expected_value, rel_tol=1e-12, abs_tol=1e-12),
            "metric value/components differ",
        )


def public_status(manifest, now):
    validate_manifest(manifest)
    now = timestamp(now)
    fresh = manifest["freshness"]
    require(
        now >= timestamp(manifest["validation_as_of"]),
        "status clock predates validation",
    )
    state = (
        "expired"
        if now >= timestamp(fresh["expires_at"])
        else "stale_usable"
        if (now - timestamp(fresh["discovery_verified_at"])).total_seconds()
        > PROFILE["freshness"]["warning_seconds"]
        else "fresh"
    )
    result = {
        key: manifest[key]
        for key in (
            "publication_id",
            "profile",
            "data_through",
            "capabilities",
            "scope",
            "evidence_kind",
            "purpose",
        )
    }
    result.update(
        version="publication-status/2"
        if manifest["version"] == "publication-manifest/2"
        else "publication-status/1",
        freshness=state,
    )
    schema_check("status", result)
    return result
