import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace

import pytest
from app.agent import serving_warehouse
from app.agent.ask_data_bundle import (
    BUNDLE_SCHEMA_VERSION,
    AskBundleWarehouse,
    AskDataBundle,
    validate_bundle_manifest,
)
from app.agent.execution import request_policy
from app.agent.performance_overview import build_overview, overview_scope
from app.agent.publication_ask_source import (
    ProductionAskWarehouse,
    PublicationAskSource,
)
from app.agent.semantic_serving import SemanticWarehouse, warehouse_for_repository
from app.agent.semantic_source import BigQuerySemanticSource, snapshot_digest
from app.agent.semantics import Evidence, Query, SemanticError, run_query
from app.agent.serving_bigquery import ServingReference, encode_publication
from app.agent.serving_contract import MetricSummary
from app.agent.serving_publication import candidate_summaries, digest
from app.agent.serving_runtime import PilotPublication
from app.agent.serving_warehouse import RefreshingServingWarehouse
from google.api_core.exceptions import NotFound
from tests.test_overview_positions import fixture as position_fixture
from tests.test_semantic_source import Client as SnapshotClient
from tests.test_semantic_source import source_rows
from tests.test_serving_bigquery import Client as WireClient
from tests.test_serving_bigquery import wire

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)
CAPTURE = NOW - timedelta(seconds=10)
DEV_EXPIRY = CAPTURE + timedelta(seconds=300)
CORE_EXPIRY = NOW + timedelta(days=2)
SEASON, PHASE = "2024-25", "Regular Season"
FIRST, LAST = "2024-10-22", "2025-04-13"


def seal_manifest(document):
    document["bundle_id"] = digest(
        {k: v for k, v in document.items() if k != "bundle_id"}
    )
    return document


@pytest.fixture
def manifest():
    return seal_manifest(
        {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "publication_id": "pub_fixture",
            "source_snapshot_id": "source_fixture",
            "scope": {
                "seasons": [SEASON],
                "phases": [PHASE],
                "first": FIRST,
                "last": LAST,
                "cohort_complete": True,
            },
            "freshness": {
                "policy": "dev_300s",
                "source_timestamp": CAPTURE.isoformat(),
                "built_at": NOW.isoformat(),
                "expires_at": DEV_EXPIRY.isoformat(),
            },
            "capabilities": ["points", "playoff_rounds"],
            "summary_digest": "b" * 64,
            "facts_digest": "c" * 64,
        }
    )


def test_schema_validates_both_policies_and_optional_slots(manifest):
    validate_bundle_manifest(manifest, now=NOW)
    manifest["freshness"].update(
        policy="authority_backed", expires_at=CORE_EXPIRY.isoformat()
    )
    manifest["auxiliary"] = {
        slot: {"digest": "d" * 64, "record_count": count}
        for count, slot in enumerate(
            (
                "playoff_rounds",
                "season_positions",
                "player_aliases",
                "event_participation",
            )
        )
    }
    validate_bundle_manifest(seal_manifest(manifest), now=DEV_EXPIRY)


@pytest.mark.parametrize(
    ("section", "change"),
    [
        (None, {"extra": "field"}),
        (None, {"schema_version": "ask_data_bundle/2"}),
        (None, {"bundle_id": "not-a-sha256"}),
        (None, {"capabilities": ["points", "points"]}),
        (None, {"capabilities": ["unknown"]}),
        (None, {"auxiliary": {"unknown": {"digest": "d" * 64, "record_count": 1}}}),
        (
            None,
            {"auxiliary": {"player_aliases": {"digest": "d" * 64, "record_count": -1}}},
        ),
        (
            None,
            {
                "auxiliary": {
                    "player_aliases": {
                        "digest": "d" * 64,
                        "record_count": 1,
                        "extra": True,
                    }
                }
            },
        ),
        ("scope", {"extra": True}),
        ("scope", {"first": "2025-05-01"}),
        ("scope", {"last": "2025-02-30"}),
        ("freshness", {"extra": True}),
        ("freshness", {"source_timestamp": CAPTURE.replace(tzinfo=None).isoformat()}),
        ("freshness", {"built_at": (CAPTURE - timedelta(seconds=1)).isoformat()}),
        ("freshness", {"expires_at": NOW.isoformat()}),
        (
            "freshness",
            {"expires_at": (DEV_EXPIRY + timedelta(microseconds=1)).isoformat()},
        ),
    ],
)
def test_schema_rejects_invalid_manifests(manifest, section, change):
    (manifest if section is None else manifest[section]).update(change)
    if "bundle_id" not in change:
        seal_manifest(manifest)
    with pytest.raises(SemanticError) as error:
        validate_bundle_manifest(manifest)
    assert error.value.code == "invalid_evidence"


def test_manifest_rejects_tampered_scope_with_original_bundle_id(manifest):
    manifest["scope"]["last"] = "2025-04-12"
    with pytest.raises(SemanticError) as error:
        validate_bundle_manifest(manifest, now=NOW)
    assert error.value.code == "invalid_evidence"


@pytest.mark.parametrize(
    "change",
    [
        {"seasons": [SEASON, "2023-24"]},
        {"phases": ["Playoffs"]},
        {
            "covered_scopes": [
                {"season": SEASON, "phase": PHASE, "data_through": "2025-02-30"}
            ]
        },
        {
            "covered_scopes": [
                dict(season=SEASON, phase=PHASE, data_through=LAST),
                dict(season=SEASON, phase=PHASE, data_through=FIRST),
            ]
        },
        {
            "covered_scopes": [
                dict(season=SEASON, phase=PHASE, data_through="2024-10-21")
            ]
        },
        {
            "covered_scopes": [
                dict(season=SEASON, phase=PHASE, data_through="2025-04-14")
            ]
        },
    ],
)
def test_manifest_rejects_invalid_covered_scopes(manifest, change):
    scope = manifest["scope"]
    scope["covered_scopes"] = [dict(season=SEASON, phase=PHASE, data_through=LAST)]
    scope.update(change)
    with pytest.raises(SemanticError) as error:
        validate_bundle_manifest(seal_manifest(manifest), now=NOW)
    assert error.value.code == "invalid_evidence"


@pytest.mark.parametrize(
    "changes",
    [
        {"capabilities": ["rankings"], "scope": {"cohort_complete": False}},
        {"capabilities": ["rankings"], "facts_digest": digest(None)},
        {"capabilities": ["detail"], "facts_digest": digest(None)},
        {"capabilities": ["rich"], "facts_digest": digest(None)},
    ],
)
def test_manifest_rejects_inconsistent_capabilities(manifest, changes):
    for key, value in changes.items():
        if isinstance(value, dict):
            manifest[key].update(value)
        else:
            manifest[key] = value
    with pytest.raises(SemanticError) as error:
        validate_bundle_manifest(seal_manifest(manifest), now=NOW)
    assert error.value.code == "invalid_evidence"


def test_manifest_rejects_covered_scope_data_through_outside_season_bounds(manifest):
    scope = manifest["scope"]
    scope["seasons"] = ["2023-24", SEASON]
    scope["first"] = "2023-10-24"
    scope["covered_scopes"] = [
        dict(season="2023-24", phase=PHASE, data_through="2025-04-01"),
        dict(season=SEASON, phase=PHASE, data_through=LAST),
    ]
    with pytest.raises(SemanticError) as error:
        validate_bundle_manifest(seal_manifest(manifest), now=NOW)
    assert error.value.code == "invalid_evidence"


@pytest.mark.parametrize("now", [DEV_EXPIRY, CAPTURE, NOW.replace(tzinfo=None)])
def test_schema_read_clock_fails_closed(manifest, now):
    with pytest.raises(SemanticError) as error:
        validate_bundle_manifest(manifest, now=now)
    assert error.value.code == "invalid_evidence"


@pytest.fixture
def core_source():
    scope = (SEASON, PHASE)
    evidence = Evidence(
        rows=[
            dict(
                season=SEASON,
                season_type=PHASE,
                player_id=1,
                player_name="Example Player",
                game_id=game_id,
                game_date=day,
                pts=points,
                fgm=made,
                fga=attempted,
            )
            for game_id, day, points, made, attempted in (
                ("001", FIRST, 0, 0, 1),
                ("002", "2024-10-24", None, 1, 2),
                ("003", LAST, 12, 2, 2),
            )
        ],
        covered_scopes=frozenset({scope}),
        source="fixture verified publication",
        snapshot_id="core_snapshot",
        data_through={scope: LAST},
        complete=True,
        cohort_complete=True,
    )
    return PublicationAskSource(
        evidence,
        {
            "publication_id": "core_a",
            "scope": dict(season=SEASON, phase=PHASE, first=FIRST, last=LAST),
            "freshness": "stale_usable",
            "cohort_proof": "publication_manifest",
        },
        source_timestamp=CAPTURE.isoformat(),
        built_at=NOW.isoformat(),
        expires_at=CORE_EXPIRY.isoformat(),
    )


@pytest.mark.parametrize("cohort_complete", [True, False])
def test_core_publication_preserves_values_and_provenance(core_source, cohort_complete):
    evidence = replace(core_source.evidence, cohort_complete=cohort_complete)
    source = replace(core_source, evidence=evidence)
    bundle = AskDataBundle.from_core_publication(
        source, now=NOW, metrics=["pts", "fg_pct"]
    )
    assert bundle.summaries == (
        MetricSummary(
            "core_a",
            SEASON,
            PHASE,
            1,
            "fg_pct",
            ("001", "002", "003"),
            ("001", "002", "003"),
            {"fgm": 3.0, "fga": 5.0},
            {"fgm": (3.0,), "fga": (5.0,)},
        ),
        MetricSummary(
            "core_a",
            SEASON,
            PHASE,
            1,
            "pts",
            ("001", "002", "003"),
            ("001", "003"),
            {"pts": 12.0},
            {"pts": (12.0,)},
        ),
    )
    capabilities = {"points", "metrics", "detail", "rich"}
    if cohort_complete:
        capabilities.add("rankings")
        assert bundle.summaries == candidate_summaries(
            evidence, "core_a", ["pts", "fg_pct"]
        )
    else:
        with pytest.raises(SemanticError, match="complete source cohort"):
            candidate_summaries(evidence, "core_a", ["pts"])
    assert set(bundle.manifest["capabilities"]) == capabilities
    assert bundle.manifest["scope"]["cohort_complete"] is cohort_complete
    assert bundle.manifest["scope"]["covered_scopes"] == (
        dict(season=SEASON, phase=PHASE, data_through=LAST),
    )
    assert bundle.manifest["freshness"] == {
        "policy": "authority_backed",
        "source_timestamp": source.source_timestamp,
        "built_at": source.built_at,
        "expires_at": source.expires_at,
    }
    assert bundle.manifest["source_snapshot_id"] == evidence.snapshot_id
    assert bundle.provenance == source.provenance
    query = Query("pts", SEASON, "total", player_id=1)
    assert run_query(bundle.evidence, query) == run_query(
        source.load([SEASON])[1], query
    )
    validate_bundle_manifest(bundle.manifest, now=NOW)


def test_frozen_bundle_preserves_position_group_cohort(core_source):
    grouped, context = position_fixture()
    evidence = replace(
        core_source.evidence,
        rows=[row for rows in grouped.values() for row in rows],
        position_context=context,
    )
    bundle = AskDataBundle.from_core_publication(
        replace(core_source, evidence=evidence), now=NOW, metrics=["pts"]
    )
    question = "How was Example 1's scoring performance in the 2024-25 regular season among guards?"
    scope = overview_scope(question, SEASON)
    players = [{"player_id": 1, "player_name": "Example 1"}]

    def points_comparison(source):
        result = build_overview(question, source, players, scope)
        points = next(
            metric
            for metric in result["semantic_evidence"]["metrics"]
            if metric["key"] == "pts"
        )
        return points["peer_comparison"]

    original = points_comparison(evidence)
    assert original["kind"] == "position"
    assert original["group"] == "Guard"
    assert original["cohort_size"] == 20
    assert original["evidence_id"] == context["sha256"]
    assert points_comparison(bundle.evidence) == original


def test_bundle_is_immutable_and_digests_are_canonical(core_source):
    bundle = AskDataBundle.from_core_publication(
        core_source, now=NOW, metrics=["pts", "fg_pct"]
    )
    reversed_source = replace(
        core_source,
        evidence=replace(core_source.evidence, rows=core_source.evidence.rows[::-1]),
    )
    reordered = AskDataBundle.from_core_publication(
        reversed_source, now=NOW + timedelta(seconds=1), metrics=["fg_pct", "pts"]
    )
    assert bundle.manifest == reordered.manifest
    with pytest.raises(FrozenInstanceError):
        bundle.evidence = None
    with pytest.raises(TypeError):
        bundle.manifest["freshness"]["expires_at"] = NOW.isoformat()
    with pytest.raises(TypeError):
        bundle.summaries[0].components["fgm"] = 5
    with pytest.raises(TypeError):
        bundle.evidence.rows[0]["pts"] = 10
    core_source.evidence.rows[0]["pts"] = 10
    core_source.status["freshness"] = "expired"
    assert bundle.evidence.rows[0]["pts"] == 0
    assert bundle.provenance["freshness"] == "stale_usable"
    core_source.status["freshness"] = "fresh"
    changed = AskDataBundle.from_core_publication(
        core_source, now=NOW, metrics=["pts", "fg_pct"]
    )
    for key in ("bundle_id", "summary_digest", "facts_digest"):
        assert changed.manifest[key] != bundle.manifest[key]
    for tampered in (
        dict(summaries=()),
        dict(evidence=None),
        dict(summaries=(), evidence=None),
    ):
        with pytest.raises(ValueError, match="digest"):
            replace(bundle, **tampered)
    bad_manifest = dict(bundle.manifest)
    bad_manifest["publication_id"] = "tampered"
    with pytest.raises(SemanticError) as error:
        replace(bundle, manifest=bad_manifest)
    assert error.value.code == "invalid_evidence"
    for tampered_evidence in (
        replace(bundle.evidence, cohort_complete=False),
        replace(bundle.evidence, snapshot_id="other_snapshot"),
        replace(
            bundle.evidence,
            covered_scopes=frozenset({(SEASON, "Playoffs")}),
            data_through={(SEASON, "Playoffs"): LAST},
        ),
        replace(bundle.evidence, data_through={(SEASON, PHASE): FIRST}),
    ):
        with pytest.raises(SemanticError) as error:
            replace(bundle, evidence=tampered_evidence)
        assert error.value.code == "invalid_evidence"


def test_core_partial_timestamps_bound_legacy_source_timestamp_by_built_at(
    core_source,
):
    earlier_build = (NOW - timedelta(minutes=5)).isoformat()
    partial = replace(core_source, source_timestamp=None, built_at=earlier_build)
    bundle = AskDataBundle.from_core_publication(partial, now=NOW)
    assert bundle.manifest["freshness"] == {
        "policy": "authority_backed",
        "source_timestamp": earlier_build,
        "built_at": earlier_build,
        "expires_at": CORE_EXPIRY.isoformat(),
    }
    untimestamped = replace(core_source, source_timestamp=None, built_at=None)
    fallback = AskDataBundle.from_core_publication(untimestamped, now=NOW)
    assert fallback.manifest["freshness"] == {
        "policy": "authority_backed",
        "source_timestamp": NOW.isoformat(),
        "built_at": NOW.isoformat(),
        "expires_at": CORE_EXPIRY.isoformat(),
    }


def test_core_explicit_expiry_can_only_shorten_a_bound_release(core_source):
    shortened = (NOW + timedelta(minutes=1)).isoformat()
    assert (
        AskDataBundle.from_core_publication(
            core_source, now=NOW, expires_at=shortened
        ).manifest["freshness"]["expires_at"]
        == shortened
    )
    with pytest.raises(SemanticError) as error:
        AskDataBundle.from_core_publication(
            core_source,
            now=NOW,
            expires_at=(CORE_EXPIRY + timedelta(seconds=1)).isoformat(),
        )
    assert error.value.code == "invalid_evidence"


@pytest.fixture
def pilot_snapshot():
    snapshot = BigQuerySemanticSource(SnapshotClient(), project="test-project").capture(
        [SEASON]
    )
    snapshot["source_timestamp"] = CAPTURE.isoformat()
    snapshot["sha256"] = snapshot_digest(snapshot)
    return snapshot


def pilot(snapshot, *, metrics=None, detail=False, publication_id="pilot_a"):
    return PilotPublication.build(
        snapshot,
        expected_snapshot_id=snapshot["sha256"],
        publication_id=publication_id,
        relation_version=f"fixture/{publication_id}",
        now=NOW,
        metrics=metrics,
        include_detail=detail,
    )


@pytest.mark.parametrize(
    ("metrics", "detail", "capabilities"),
    [
        (None, False, {"points"}),
        (["ast"], False, {"metrics"}),
        (["ast"], True, {"points", "metrics", "rankings", "detail", "rich"}),
        (["pts", "ast"], False, {"points", "metrics"}),
        (["pts", "ast"], True, {"points", "metrics", "rankings", "detail", "rich"}),
    ],
)
def test_pilot_publication_preserves_approved_summaries(
    pilot_snapshot, metrics, detail, capabilities
):
    publication = pilot(pilot_snapshot, metrics=metrics, detail=detail)
    bundle = AskDataBundle.from_pilot_publication(publication, now=NOW)
    assert set(bundle.manifest["capabilities"]) == capabilities
    assert bundle.manifest["scope"]["seasons"] == (SEASON,)
    assert bundle.manifest["scope"]["cohort_complete"] is True
    assert bundle.manifest["freshness"]["policy"] == "dev_300s"
    assert bundle.manifest["freshness"]["source_timestamp"] == CAPTURE.isoformat()
    assert bundle.manifest["freshness"]["expires_at"] == DEV_EXPIRY.isoformat()
    assert bundle.summaries == publication.summaries
    assert bundle.manifest["summary_digest"] == publication.approval.summary_digest
    publication.approval.validate_contract_binding(
        bundle.summaries,
        relation_version=publication.approval.relation_version,
        now=NOW,
    )
    if detail:
        evidence = publication.detail_evidence(now=NOW)
        assert bundle.evidence.rows == tuple(evidence.rows)
        assert bundle.manifest["facts_digest"] != digest(None)
        with pytest.raises(SemanticError, match="checksum"):
            AskDataBundle.from_pilot_publication(
                replace(publication, detail_json="{}"), now=NOW
            )
    else:
        assert "rankings" not in bundle.manifest["capabilities"]
        assert bundle.evidence is None
        assert bundle.manifest["facts_digest"] == digest(None)
    with pytest.raises(SemanticError) as error:
        AskDataBundle.from_pilot_publication(publication, now=DEV_EXPIRY)
    assert error.value.code == "invalid_evidence"


def test_summary_only_wire_preserves_short_lifetime_capture_and_legacy_fallback(
    pilot_snapshot,
):
    publication = pilot(pilot_snapshot, metrics=["pts", "ast"], detail=False)
    short_expiry = NOW + timedelta(seconds=120)
    short_pub = replace(
        publication, approval=replace(publication.approval, expires_at=short_expiry)
    )
    bundle = AskDataBundle.from_pilot_publication(short_pub, now=NOW)
    assert bundle.manifest["freshness"]["source_timestamp"] == CAPTURE.isoformat()
    assert bundle.manifest["freshness"]["expires_at"] == short_expiry.isoformat()

    legacy_metadata = json.loads(short_pub.metadata_json)
    legacy_metadata.pop("source_timestamp")
    legacy_json = json.dumps(legacy_metadata, sort_keys=True, separators=(",", ":"))
    legacy_pub = replace(
        short_pub, metadata_json=legacy_json, metadata_digest=digest(legacy_metadata)
    )
    legacy_bundle = AskDataBundle.from_pilot_publication(legacy_pub, now=NOW)
    assert (
        legacy_bundle.manifest["freshness"]["source_timestamp"]
        == (short_expiry - timedelta(seconds=300)).isoformat()
    )

    detail_pub = pilot(pilot_snapshot, metrics=["pts", "ast"], detail=True)
    mismatched_metadata = json.loads(detail_pub.metadata_json)
    mismatched_metadata["source_timestamp"] = (
        CAPTURE - timedelta(seconds=5)
    ).isoformat()
    mismatched_json = json.dumps(
        mismatched_metadata, sort_keys=True, separators=(",", ":")
    )
    with pytest.raises(SemanticError) as error:
        AskDataBundle.from_pilot_publication(
            replace(
                detail_pub,
                metadata_json=mismatched_json,
                metadata_digest=digest(mismatched_metadata),
            ),
            now=NOW,
        )
    assert error.value.code == "invalid_evidence"


def test_pilot_bundle_preserves_sparse_season_phase_coverage():
    rows = source_rows()
    for row in rows:
        if row["season_type"] == PHASE:
            row["season"] = "2023-24"
            row["game_date"] = row["game_date"].replace("2025-", "2024-")
    snapshot = BigQuerySemanticSource(
        SnapshotClient(rows), project="test-project"
    ).capture(["2023-24", "2024-25"])
    snapshot["source_timestamp"] = CAPTURE.isoformat()
    snapshot["sha256"] = snapshot_digest(snapshot)
    publication = pilot(snapshot)
    scope = AskDataBundle.from_pilot_publication(publication, now=NOW).manifest["scope"]
    assert scope["seasons"] == ("2023-24", "2024-25")
    assert scope["phases"] == ("Playoffs", "Regular Season")
    assert scope["covered_scopes"] == tuple(
        dict(season=season, phase=phase, data_through=day)
        for season, phase, day in (
            ("2023-24", "Regular Season", "2024-01-07"),
            ("2024-25", "Playoffs", "2025-04-01"),
        )
    )


@pytest.fixture
def settings():
    return SimpleNamespace(
        agent_request_deadline_seconds=20,
        agent_max_tool_calls=1,
        ask_local_espn_dev=False,
    )


def test_request_pins_core_source_and_rechecks_authority(
    core_source, settings, monkeypatch
):
    next_source = replace(
        core_source, status={**core_source.status, "publication_id": "core_b"}
    )
    reads = []
    active = core_source

    class Reader:
        def read(self, publication_id):
            reads.append(publication_id)
            return active

    reader = Reader()
    monkeypatch.setattr(AskBundleWarehouse, "_utc_now", staticmethod(lambda: NOW))
    facts = SemanticWarehouse(SnapshotClient())
    repo = SimpleNamespace(
        settings=settings,
        _governed_warehouse=facts,
        ask_publication_source=reader.read,
    )
    warehouse = warehouse_for_repository(repo)
    assert isinstance(warehouse, AskBundleWarehouse)
    assert ProductionAskWarehouse is AskBundleWarehouse
    assert warehouse.historical is facts
    assert warehouse.players == facts.players
    with request_policy(settings, "openai"):
        assert warehouse.open_publication() is core_source
        active = next_source
        repo.ask_publication_source = reader.read
        reused = warehouse_for_repository(repo)
        assert reused is warehouse
        bundle = reused.open_bundle("core_a", now=NOW)
        assert warehouse.open_bundle(now=NOW) is bundle
        assert warehouse.open_publication("core_a") is core_source
        with pytest.raises(SemanticError) as error:
            warehouse.open_bundle("core_b", now=NOW)
        assert error.value.code == "publication_unavailable"
        with pytest.raises(SemanticError) as error:
            warehouse.open_bundle(now=CORE_EXPIRY)
        assert error.value.code == "invalid_evidence"
        assert reads == [None]
        with pytest.raises(SemanticError) as error:
            warehouse.recheck_publication(core_source)
        assert error.value.code == "publication_unavailable"
        assert reads == [None, "core_a"]
    with request_policy(settings, "openai"):
        assert warehouse.open_bundle(now=NOW).manifest["publication_id"] == "core_b"
        assert warehouse.open_publication() is next_source
    repo.ask_publication_source = Reader().read
    assert warehouse_for_repository(repo) is not warehouse


def test_core_authority_clock_blocks_expired_reads_even_with_explicit_now(
    core_source, settings
):
    clock = NOW
    warehouse = AskBundleWarehouse(
        SemanticWarehouse(SnapshotClient()),
        lambda _: core_source,
        clock=lambda: clock,
    )
    with request_policy(settings, "openai") as policy:
        bundle = warehouse.open_bundle(now=NOW)
        assert warehouse.open_bundle(now=NOW) is bundle
        clock = CORE_EXPIRY
        with pytest.raises(SemanticError) as error:
            warehouse.open_bundle(now=NOW)
        assert error.value.code == "invalid_evidence"
    clock = CORE_EXPIRY
    with request_policy(settings, "openai") as policy:
        with pytest.raises(SemanticError) as error:
            warehouse.open_bundle(now=NOW)
        assert error.value.code == "invalid_evidence"
        assert "bundle" not in policy.serving_state[warehouse]


def test_concurrent_bundle_reads_share_the_request_pin(core_source, settings):
    entered = Event()
    release = Event()
    reads = []

    def read(publication_id):
        reads.append(publication_id)
        entered.set()
        assert release.wait(timeout=5)
        return core_source

    warehouse = AskBundleWarehouse(
        SemanticWarehouse(SnapshotClient()), read, clock=lambda: NOW
    )
    with request_policy(settings, "openai"), ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(copy_context().run, warehouse.open_bundle, now=NOW)
        try:
            assert entered.wait(timeout=5)
            second = pool.submit(
                copy_context().run, warehouse.open_bundle, "core_a", now=NOW
            )
        finally:
            release.set()
        assert first.result(timeout=5) is second.result(timeout=5)
    assert reads == [None]


@pytest.mark.parametrize(
    "binding",
    ["publication_id", "snapshot_id", "expires_at", "built_at", "source_timestamp"],
)
def test_core_bundle_cache_reuses_builds_and_tracks_authority(
    core_source, settings, monkeypatch, binding
):
    active = core_source
    reads = []
    builds = []
    build = AskDataBundle.from_core_publication

    def read(publication_id):
        reads.append(publication_id)
        return active

    def count_build(cls, source, **kwargs):
        builds.append(source)
        return build(source, **kwargs)

    monkeypatch.setattr(
        AskDataBundle, "from_core_publication", classmethod(count_build)
    )
    warehouse = AskBundleWarehouse(
        SemanticWarehouse(SnapshotClient()), read, clock=lambda: NOW
    )
    expected = build(core_source, now=NOW)
    with request_policy(settings, "openai"):
        original = warehouse.open_bundle(now=NOW)
        assert original.manifest == expected.manifest
    with request_policy(settings, "openai"):
        assert warehouse.open_bundle(now=NOW) is original
    with request_policy(settings, "openai"):
        with pytest.raises(SemanticError) as error:
            warehouse.open_bundle(now=CORE_EXPIRY)
        assert error.value.code == "invalid_evidence"
    assert builds == [core_source]

    if binding == "publication_id":
        active = replace(active, status=dict(active.status, publication_id="core_b"))
    elif binding == "snapshot_id":
        active = replace(
            active, evidence=replace(active.evidence, snapshot_id="core_v2")
        )
    else:
        bound_times = dict(
            expires_at=CORE_EXPIRY - timedelta(days=1),
            built_at=NOW - timedelta(seconds=1),
            source_timestamp=CAPTURE - timedelta(seconds=1),
        )
        active = replace(active, **{binding: bound_times[binding].isoformat()})
    with request_policy(settings, "openai"):
        refreshed = warehouse.open_bundle(now=NOW)
        assert refreshed.manifest["bundle_id"] != original.manifest["bundle_id"]
    assert builds == [core_source, active]
    assert reads == [None] * 4


def test_cold_build_crossing_expiry_is_neither_returned_nor_cached(
    core_source, settings, monkeypatch
):
    warehouse = AskBundleWarehouse(
        SemanticWarehouse(SnapshotClient()), lambda _: core_source
    )
    times = iter([NOW, CORE_EXPIRY])
    monkeypatch.setattr(warehouse, "_utc_now", lambda: next(times))
    with request_policy(settings, "openai") as policy:
        with pytest.raises(SemanticError) as error:
            warehouse.open_bundle()
        assert error.value.code == "invalid_evidence"
        assert "bundle" not in policy.serving_state[warehouse]


def test_concurrent_prepare_and_open_bundle_share_wire_pin(
    pilot_snapshot, settings, tmp_path, monkeypatch
):
    first, first_records = wire(pilot(pilot_snapshot))
    second_table = "ask_serving_" + "b" * 32
    header, second_records = encode_publication(
        pilot(pilot_snapshot, publication_id="pilot_b"),
        now=NOW,
        relation_id=second_table,
    )
    second = replace(first, table=second_table, manifest_digest=digest(header))
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(asdict(first)))

    class VersionedClient(WireClient):
        def query(self, sql, **kwargs):
            self.records = second_records if second.table in sql else first_records
            return super().query(sql, **kwargs)

    warehouse = RefreshingServingWarehouse(
        SemanticWarehouse(SnapshotClient()),
        VersionedClient(first_records),
        path,
        lambda: NOW,
        project=first.project,
        mode="serve",
    )
    first_read, release_first, prepare_waiting_or_done = Event(), Event(), Event()
    relation = ServingReference.relation

    def pause_first_read(reference):
        if reference.table == first.table and not first_read.is_set():
            first_read.set()
            assert release_first.wait(timeout=5)
        return relation(reference)

    monkeypatch.setattr(ServingReference, "relation", pause_first_read)

    def planner(_players, _teams):
        return {"status": "compare"}

    with (
        request_policy(settings, "openai") as policy,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        acquire_lock = serving_warehouse.acquire_timed_lock

        def observe_prepare_lock(lock, *args):
            if lock is policy.bundle_lock and first_read.is_set():
                prepare_waiting_or_done.set()
            return acquire_lock(lock, *args)

        monkeypatch.setattr(
            serving_warehouse, "acquire_timed_lock", observe_prepare_lock
        )
        bundle_read = pool.submit(copy_context().run, warehouse.open_bundle)
        try:
            assert first_read.wait(timeout=5)
            promoted = tmp_path / "promoted.json"
            promoted.write_text(json.dumps(asdict(second)))
            promoted.replace(path)
            prepared = pool.submit(
                copy_context().run, warehouse.prepare, planner, seasons=[SEASON]
            )
            prepared.add_done_callback(lambda _: prepare_waiting_or_done.set())
            assert prepare_waiting_or_done.wait(timeout=5)
        finally:
            release_first.set()
        bundle = bundle_read.result(timeout=5)
        attempt = prepared.result(timeout=5)
        assert (
            attempt.source_query["publication_id"]
            == bundle.manifest["publication_id"]
            == "pilot_a"
        )
    with request_policy(settings, "openai"):
        assert warehouse.open_bundle().manifest["publication_id"] == "pilot_b"


def test_refreshing_warehouse_exposes_wire_bundle_and_keeps_core_separate(
    pilot_snapshot, core_source, settings, tmp_path, monkeypatch
):
    publication = pilot(pilot_snapshot, detail=True)
    reference, records = wire(publication)
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(asdict(reference)))
    client = WireClient(records)
    clock = NOW
    historical = RefreshingServingWarehouse(
        SemanticWarehouse(SnapshotClient()),
        client,
        path,
        lambda: clock,
        project=reference.project,
        mode="serve",
    )
    monkeypatch.setattr(AskBundleWarehouse, "_utc_now", staticmethod(lambda: NOW))
    repo = SimpleNamespace(
        settings=settings,
        _governed_warehouse=historical,
        ask_publication_source=lambda _: core_source,
    )
    warehouse = warehouse_for_repository(repo)
    assert warehouse.historical is historical
    assert warehouse.open_bundle(now=NOW).manifest["publication_id"] == "core_a"
    assert client.calls == []
    repo.ask_publication_source = None
    assert warehouse_for_repository(repo) is historical
    with request_policy(settings, "openai"):
        bundle = historical.open_bundle()
        assert bundle.summaries == publication.summaries
        assert "detail" in bundle.manifest["capabilities"]
        assert len(client.calls) == 2
        path.unlink()
        assert AskBundleWarehouse(historical).open_bundle("pilot_a") is bundle
        with pytest.raises(SemanticError) as error:
            historical.open_bundle("different")
        assert error.value.code == "publication_unavailable"
        clock = DEV_EXPIRY
        with pytest.raises(SemanticError) as error:
            historical.open_bundle()
        assert error.value.code == "invalid_evidence"
        with pytest.raises(SemanticError) as error:
            historical.open_bundle(now=NOW)
        assert error.value.code == "invalid_evidence"
    with pytest.raises(SemanticError, match="reference unavailable"):
        historical.open_bundle(now=NOW)
    assert len(client.calls) == 2


@pytest.mark.parametrize("shadow_first", [False, True])
def test_wire_open_bundle_and_lazy_shadow_share_lock_order(
    pilot_snapshot, settings, tmp_path, monkeypatch, shadow_first
):
    from app.agent import ask_data_bundle
    from app.agent.serving_bigquery import BigQueryServingReader

    publication = pilot(pilot_snapshot)
    reference, records = wire(publication)
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(asdict(reference)))
    entered, peer_waiting, release = Event(), Event(), Event()
    original_load = BigQueryServingReader.load

    def paused_load(self, *, now, include_detail=False):
        if not entered.is_set():
            entered.set()
            assert release.wait(timeout=5)
        return original_load(self, now=now, include_detail=include_detail)

    monkeypatch.setattr(BigQueryServingReader, "load", paused_load)
    client = WireClient(records)
    warehouse = RefreshingServingWarehouse(
        SemanticWarehouse(
            BigQuerySemanticSource(SnapshotClient(), project="test-project")
        ),
        client,
        path,
        lambda: NOW,
        project=reference.project,
        mode="shadow",
    )
    query = Query(metric="pts", aggregation="average", season=SEASON, player_id=1)
    with (
        request_policy(settings, "openai") as policy,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        acquire_lock = serving_warehouse.acquire_timed_lock

        def observe_lock(lock, *args):
            if entered.is_set() and lock is policy.bundle_lock:
                peer_waiting.set()
            return acquire_lock(lock, *args)

        monkeypatch.setattr(serving_warehouse, "acquire_timed_lock", observe_lock)
        monkeypatch.setattr(ask_data_bundle, "acquire_timed_lock", observe_lock)
        _, evidence = warehouse.load([SEASON])
        shadow = warehouse.serving_shadow
        reference_result = run_query(evidence, query)
        first = (
            pool.submit(copy_context().run, shadow.compare, query, reference_result)
            if shadow_first
            else pool.submit(copy_context().run, warehouse.open_bundle)
        )
        try:
            assert entered.wait(timeout=5)
            second = (
                pool.submit(copy_context().run, warehouse.open_bundle)
                if shadow_first
                else pool.submit(
                    copy_context().run, shadow.compare, query, reference_result
                )
            )
            assert peer_waiting.wait(timeout=5)
        finally:
            release.set()
        if shadow_first:
            first.result(timeout=5)
            bundle = second.result(timeout=5)
        else:
            bundle = first.result(timeout=5)
            second.result(timeout=5)
    assert bundle.manifest["publication_id"] == "pilot_a"
    assert shadow.outcomes == [
        {"status": "match", "code": "capture_provenance_differs"}
    ]
    assert len(client.calls) == 1


def test_missing_wire_relation_fails_closed(pilot_snapshot, tmp_path):
    publication = pilot(pilot_snapshot)
    reference, _ = wire(publication)
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(asdict(reference)))

    class MissingRelation:
        def query(self, *_args, **_kwargs):
            raise NotFound("relation unavailable")

    warehouse = RefreshingServingWarehouse(
        SemanticWarehouse(SnapshotClient()),
        MissingRelation(),
        path,
        lambda: NOW,
        project=reference.project,
        mode="serve",
    )
    with pytest.raises(SemanticError) as error:
        warehouse.open_bundle()
    assert error.value.code == "publication_unavailable"


def test_warehouse_fails_closed_for_unavailable_or_expired_core(core_source):
    facts = SemanticWarehouse(SnapshotClient())

    def unavailable(_):
        raise OSError("offline")

    for reader in (None, unavailable, lambda _: None):
        with pytest.raises(SemanticError) as error:
            AskBundleWarehouse(facts, reader, clock=lambda: NOW).open_bundle(now=NOW)
        assert error.value.code == "publication_unavailable"
    warehouse = AskBundleWarehouse(facts, lambda _: core_source, clock=lambda: NOW)
    with pytest.raises(SemanticError) as error:
        warehouse.open_bundle("absent", now=NOW)
    assert error.value.code == "publication_unavailable"
    with pytest.raises(SemanticError) as error:
        warehouse.open_bundle(now=CORE_EXPIRY)
    assert error.value.code == "invalid_evidence"
    legacy = replace(core_source, expires_at=None)
    with pytest.raises(SemanticError) as error:
        AskBundleWarehouse(facts, lambda _: legacy, clock=lambda: NOW).open_bundle(
            now=NOW
        )
    assert error.value.code == "invalid_evidence"


def test_lazy_shadow_starts_optional_deadline_before_bundle_lock(
    pilot_snapshot, settings, tmp_path, monkeypatch
):
    from app.agent import execution
    from app.agent.execution import CURRENT

    publication = pilot(pilot_snapshot)
    reference, records = wire(publication)
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(asdict(reference)))
    now_monotonic = [100.0]
    monkeypatch.setattr(execution, "monotonic", lambda: now_monotonic[0])
    monkeypatch.setattr(serving_warehouse, "monotonic", lambda: now_monotonic[0])

    observed_deadlines = []
    client = WireClient(records)
    warehouse = RefreshingServingWarehouse(
        SemanticWarehouse(
            BigQuerySemanticSource(SnapshotClient(), project="test-project")
        ),
        client,
        path,
        lambda: NOW,
        project=reference.project,
        mode="shadow",
    )
    query = Query(metric="pts", aggregation="average", season=SEASON, player_id=1)

    acquire_lock = serving_warehouse.acquire_timed_lock

    def slow_bundle_lock(lock, *args):
        result = acquire_lock(lock, *args)
        if lock is policy.bundle_lock:
            if not observed_deadlines:
                now_monotonic[0] += 2.0
            observed_deadlines.append(("lock", CURRENT.get().deadline))
        return result

    monkeypatch.setattr(serving_warehouse, "acquire_timed_lock", slow_bundle_lock)
    original_compare = serving_warehouse.ServingWarehouse.candidate_shadow

    def observe_compare(self, evidence=None):
        observed_deadlines.append(("compare", CURRENT.get().deadline))
        return original_compare(self, evidence)

    monkeypatch.setattr(
        serving_warehouse.ServingWarehouse, "candidate_shadow", observe_compare
    )

    short_settings = SimpleNamespace(
        agent_request_deadline_seconds=5,
        agent_max_tool_calls=1,
        ask_local_espn_dev=False,
    )
    with request_policy(short_settings, "openai") as policy:
        _, evidence = warehouse.load([SEASON])
        shadow = warehouse.serving_shadow
        reference_result = run_query(evidence, query)
        # Foreground remaining is 5.0s; optional budget reserves 1.0s -> deadline 104.0.
        shadow.compare(query, reference_result)
        assert observed_deadlines == [
            ("lock", 104.0),
            ("lock", 104.0),
            ("compare", 104.0),
        ]
        assert shadow.outcomes == [
            {"status": "match", "code": "capture_provenance_differs"}
        ]
        # Remaining <= 1.0s fails immediately with optional_timeout before bundle_lock.
        now_monotonic[0] = 104.0
        observed_deadlines.clear()
        shadow.compare(query, reference_result)
        assert observed_deadlines == []
        assert shadow.outcomes[-1] == {"status": "error", "code": "optional_timeout"}


def test_bundle_post_init_validates_auxiliary_slots_against_manifest(core_source):
    from app.agent.ask_data_bundle import _json_value
    from tests.test_overview_positions import fixture as position_fixture
    from tests.test_playoff_rounds_and_positions import (
        position_context,
        season_positions,
    )

    _, legacy = position_fixture()
    context = position_context(season_positions(legacy))
    bundle = AskDataBundle.from_core_publication(
        core_source, now=NOW, metrics=["pts"], season_positions=context
    )
    records = list(bundle.auxiliary["season_positions"])
    slot = dict(bundle.manifest["auxiliary"]["season_positions"])

    def seal(manifest):
        updated = _json_value(manifest)
        updated.pop("bundle_id", None)
        updated["bundle_id"] = digest(updated)
        return updated

    with pytest.raises(ValueError, match="differ from manifest"):
        AskDataBundle(
            bundle.manifest, bundle.summaries, bundle.evidence, bundle.provenance, {}
        )
    with pytest.raises(ValueError, match="record_count"):
        AskDataBundle(
            seal(
                {
                    **bundle.manifest,
                    "auxiliary": {"season_positions": {**slot, "record_count": 99}},
                }
            ),
            bundle.summaries,
            bundle.evidence,
            bundle.provenance,
            {"season_positions": records},
        )
    with pytest.raises(ValueError, match="digest"):
        AskDataBundle(
            seal(
                {
                    **bundle.manifest,
                    "auxiliary": {"season_positions": {**slot, "digest": "a" * 64}},
                }
            ),
            bundle.summaries,
            bundle.evidence,
            bundle.provenance,
            {"season_positions": records},
        )
    plain_manifest = dict(bundle.manifest)
    plain_manifest.pop("auxiliary")
    plain_manifest["capabilities"] = [
        c for c in plain_manifest["capabilities"] if c != "season_positions"
    ]
    with pytest.raises(ValueError, match="differ from manifest"):
        AskDataBundle(
            seal(plain_manifest),
            bundle.summaries,
            bundle.evidence,
            bundle.provenance,
            {"season_positions": records},
        )
