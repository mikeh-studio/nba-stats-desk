import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace

import pytest
from app.agent.ask_data_bundle import (
    BUNDLE_SCHEMA_VERSION,
    AskBundleWarehouse,
    AskDataBundle,
    validate_bundle_manifest,
)
from app.agent.execution import request_policy
from app.agent.publication_ask_source import (
    ProductionAskWarehouse,
    PublicationAskSource,
)
from app.agent.semantic_serving import SemanticWarehouse, warehouse_for_repository
from app.agent.semantic_source import BigQuerySemanticSource, snapshot_digest
from app.agent.semantics import Evidence, Query, SemanticError, run_query
from app.agent.serving_contract import MetricSummary
from app.agent.serving_publication import candidate_summaries, digest
from app.agent.serving_runtime import PilotPublication
from app.agent.serving_warehouse import RefreshingServingWarehouse
from google.api_core.exceptions import NotFound
from tests.test_semantic_source import Client as SnapshotClient
from tests.test_serving_bigquery import Client as WireClient
from tests.test_serving_bigquery import wire

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)
CAPTURE = NOW - timedelta(seconds=10)
DEV_EXPIRY = CAPTURE + timedelta(seconds=300)
CORE_EXPIRY = NOW + timedelta(days=2)
SEASON, PHASE = "2024-25", "Regular Season"
FIRST, LAST = "2024-10-22", "2025-04-13"


@pytest.fixture
def manifest():
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "bundle_id": "a" * 64,
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
    validate_bundle_manifest(manifest, now=DEV_EXPIRY)


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
    with pytest.raises(SemanticError) as error:
        validate_bundle_manifest(manifest)
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
        (["pts", "ast"], False, {"points", "metrics", "rankings"}),
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
    if detail:
        evidence = publication.detail_evidence(now=NOW)
        assert bundle.evidence.rows == tuple(evidence.rows)
        assert bundle.manifest["facts_digest"] != digest(None)
        with pytest.raises(SemanticError, match="checksum"):
            AskDataBundle.from_pilot_publication(
                replace(publication, detail_json="{}"), now=NOW
            )
    else:
        assert bundle.evidence is None
        assert bundle.manifest["facts_digest"] == digest(None)
    with pytest.raises(SemanticError) as error:
        AskDataBundle.from_pilot_publication(publication, now=DEV_EXPIRY)
    assert error.value.code == "invalid_evidence"


@pytest.fixture
def settings():
    return SimpleNamespace(
        agent_request_deadline_seconds=20,
        agent_max_tool_calls=1,
        ask_local_espn_dev=False,
    )


def test_request_pins_core_source_and_rechecks_authority(core_source, settings):
    next_source = replace(
        core_source, status={**core_source.status, "publication_id": "core_b"}
    )
    reads = []
    active = core_source

    def read(publication_id):
        reads.append(publication_id)
        return active

    facts = SemanticWarehouse(SnapshotClient())
    repo = SimpleNamespace(
        settings=settings, _governed_warehouse=facts, ask_publication_source=read
    )
    warehouse = warehouse_for_repository(repo)
    assert isinstance(warehouse, AskBundleWarehouse)
    assert ProductionAskWarehouse is AskBundleWarehouse
    assert warehouse.historical is facts
    assert warehouse.players == facts.players
    with request_policy(settings, "openai"):
        assert warehouse.open_publication() is core_source
        active = next_source
        bundle = warehouse_for_repository(repo).open_bundle("core_a", now=NOW)
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


def test_concurrent_bundle_reads_share_the_request_pin(core_source, settings):
    entered = Event()
    release = Event()
    reads = []

    def read(publication_id):
        reads.append(publication_id)
        entered.set()
        assert release.wait(timeout=5)
        return core_source

    warehouse = AskBundleWarehouse(SemanticWarehouse(SnapshotClient()), read)
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


def test_refreshing_warehouse_exposes_wire_bundle_and_keeps_core_separate(
    pilot_snapshot, core_source, settings, tmp_path
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
    with pytest.raises(SemanticError, match="reference unavailable"):
        historical.open_bundle(now=NOW)
    assert len(client.calls) == 2


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
            AskBundleWarehouse(facts, reader).open_bundle(now=NOW)
        assert error.value.code == "publication_unavailable"
    warehouse = AskBundleWarehouse(facts, lambda _: core_source)
    with pytest.raises(SemanticError) as error:
        warehouse.open_bundle("absent", now=NOW)
    assert error.value.code == "publication_unavailable"
    with pytest.raises(SemanticError) as error:
        warehouse.open_bundle(now=CORE_EXPIRY)
    assert error.value.code == "invalid_evidence"
    legacy = replace(core_source, expires_at=None)
    with pytest.raises(SemanticError) as error:
        AskBundleWarehouse(facts, lambda _: legacy).open_bundle(now=NOW)
    assert error.value.code == "invalid_evidence"
