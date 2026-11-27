import json
import logging
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from app.agent.ask_data_bundle import AskBundleWarehouse
from app.agent.claims import validate
from app.agent.execution import request_policy
from app.agent.semantic_serving import warehouse_for_repository
from app.agent.semantics import SemanticError
from app.agent.serving_runtime import PilotPublication
from app.agent.serving_warehouse import RefreshingServingWarehouse
from app.config import ASK_SERVING_FAMILIES, Settings
from app.seasons import DEFAULT_SEASON
from app.serving.production_workers import PublicationWorkers
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.api_core.exceptions import NotFound
from scripts.ask_eval.cassettes import settings_binding
from tests.test_production_ask_source import ACTIVE, LAST, source
from tests.test_runtime_policy import policy
from tests.test_semantic_migration import settings
from tests.test_serving_bigquery import Client, wire
from tests.test_serving_expansion import make_prepared
from tests.test_serving_routing import Facts, ask
from tests.test_serving_runtime import NOW

SEASON = "2024-25"
POINTS = "Regular Player points per game in 2024-25 regular season"
BASELINE = "Compare Regular Player points against league average in 2024-25"


@pytest.fixture(autouse=True)
def clean_serving_env(monkeypatch):
    for name in (
        "ASK_SERVING_MODE",
        "ASK_SERVING_REFERENCE_PATH",
        "ASK_SERVING_FAMILIES",
        "ASK_LOCAL_ESPN_DEV",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "mode,reference,espn,expected",
    [
        (None, None, False, "off"),
        ("", None, False, "off"),
        (None, "reference.json", False, "serve"),
        ("", "reference.json", False, "serve"),
        (None, "reference.json", True, "off"),
        ("off", "reference.json", False, "off"),
        ("shadow", "reference.json", False, "shadow"),
        ("serve", "reference.json", False, "serve"),
        ("shadow", None, False, None),
        ("serve", None, False, None),
        ("serve", "reference.json", True, None),
        ("Serve", "reference.json", False, None),
    ],
)
def test_mode_resolution(monkeypatch, mode, reference, espn, expected):
    if mode is not None:
        monkeypatch.setenv("ASK_SERVING_MODE", mode)
    if reference is not None:
        monkeypatch.setenv("ASK_SERVING_REFERENCE_PATH", reference)
    if espn:
        monkeypatch.setenv("ASK_LOCAL_ESPN_DEV", "true")
    if expected is None:
        with pytest.raises(ValueError):
            Settings.from_env()
    else:
        assert Settings.from_env().ask_serving_mode == expected
        assert Settings.from_env().ask_serving_families == ("points",)
    assert (
        replace(settings(), ask_serving_reference_path=reference).ask_serving_mode
        == "off"
    )


def test_family_shorthand_and_nondefault_cassette_binding(monkeypatch):
    base = Settings.from_env()
    monkeypatch.setenv("ASK_SERVING_REFERENCE_PATH", "reference.json")
    inferred = Settings.from_env()
    assert settings_binding(base) != settings_binding(inferred)
    monkeypatch.setenv("ASK_SERVING_MODE", "shadow")
    shadow = Settings.from_env()
    assert settings_binding(shadow) != settings_binding(inferred)
    monkeypatch.setenv("ASK_SERVING_FAMILIES", "all")
    all_families = Settings.from_env()
    assert all_families.ask_serving_families == ASK_SERVING_FAMILIES
    assert settings_binding(all_families) != settings_binding(shadow)
    monkeypatch.setenv("ASK_SERVING_FAMILIES", "metrics, rich")
    assert Settings.from_env().ask_serving_families == ("metrics", "rich")


@pytest.mark.parametrize("mode", ["off", "shadow", "serve"])
def test_runtime_accepts_verified_serving_settings(mode):
    policy().validate_settings(
        replace(
            settings(),
            ask_serving_mode=mode,
            ask_serving_reference_path="reference.json",
        )
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"ask_serving_mode": "shadow"},
        {"ask_serving_mode": "serve"},
        {"ask_serving_mode": "unknown", "ask_serving_reference_path": "reference.json"},
        {"ask_local_espn_dev": True},
        {
            "ask_local_espn_dev": True,
            "ask_serving_mode": "shadow",
            "ask_serving_reference_path": "reference.json",
        },
    ],
)
def test_runtime_rejects_unbound_modes_or_local_espn(changes):
    # Exercise runtime validation independently of Settings.__post_init__.
    with pytest.raises(ValueError):
        policy().validate_settings(SimpleNamespace(**(asdict(settings()) | changes)))


@pytest.fixture(scope="module")
def prepared():
    return make_prepared()


def warehouse(tmp_path, facts, publication, mode, *, clock=lambda: NOW, records=None):
    reference, original = wire(publication)
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(asdict(reference)))
    client = Client(original if records is None else records)
    return RefreshingServingWarehouse(
        facts,
        client,
        path,
        clock,
        project=reference.project,
        mode=mode,
        families=ASK_SERVING_FAMILIES,
    )


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize(
    "question,route,detail",
    [
        (POINTS, "governed_metrics", False),
        (
            "Regular Player assists per game in 2024-25 regular season",
            "governed_metrics",
            False,
        ),
        (
            "Top 3 players by assists per game in 2024-25 regular season",
            "governed_metrics",
            False,
        ),
        (
            "Regular Player points per game in home games in 2024-25 regular season",
            "governed_metrics",
            True,
        ),
        (
            "Compare Regular Player points home vs away in 2024-25 regular season",
            "player_split",
            True,
        ),
        (
            "Regular Player vs Small Sample in 2024-25 regular season",
            "player_comparison",
            True,
        ),
        (
            "How did Regular Player perform from 2025-01-01 through 2025-01-07 in 2024-25 regular season?",
            "player_overview",
            True,
        ),
        (BASELINE, "league_baseline", True),
    ],
)
def test_endpoint_parity_across_serving_modes(
    prepared, tmp_path, caplog, suffix, question, route, detail
):
    evidence, publication = prepared
    facts = Facts(evidence)
    baseline = ask(facts, question, suffix)
    assert baseline["status"] == "ok", baseline["answer"]
    assert baseline["ask_route"]["route"] == route
    if route not in {"player_overview", "player_comparison"}:
        assert baseline["claim_bindings"]["claims"]

    for mode in ("shadow", "serve"):
        facts = Facts(evidence)
        candidate = warehouse(tmp_path, facts, publication, mode)
        with caplog.at_level(logging.INFO, logger="app.agent"):
            response = ask(candidate, question, suffix)
        assert response["status"] == "ok", response["answer"]
        for field in ("semantic_evidence", "tables", "charts"):
            assert response[field] == baseline[field], field
        assert response.get("claim_bindings") == baseline.get("claim_bindings")
        assert validate(response) == validate(baseline)
        assert [
            "WHERE kind = 'detail'" in sql for sql, _ in candidate.client.calls
        ] == ([False, True] if detail else [False])
        if mode == "serve":
            assert facts.calls == []
        else:
            assert facts.calls == [(SEASON,)]
            outcomes = [
                item
                for entry in caplog.records
                if entry.name == "app.agent"
                and entry.message.startswith('{"ask_route"')
                for item in json.loads(entry.message).get("serving_shadow", [])
            ]
            assert outcomes == [{"status": "match"}]


@pytest.mark.parametrize(
    "broken",
    [
        "expired",
        "missing_reference",
        "missing_relation",
        "corrupt_core",
        "corrupt_detail",
    ],
)
def test_broken_candidate_fails_closed(prepared, tmp_path, broken):
    evidence, publication = prepared
    _, records = wire(publication)
    if broken.startswith("corrupt"):
        kind = "detail" if broken == "corrupt_detail" else "summary"
        next(r for r in records if r["kind"] == kind)["record_json"] = "{}"
    facts = Facts(evidence)
    candidate = warehouse(
        tmp_path,
        facts,
        publication,
        "serve",
        records=records,
        clock=lambda: publication.approval.expires_at if broken == "expired" else NOW,
    )
    if broken == "missing_reference":
        candidate.path.unlink()
    if broken == "missing_relation":

        def missing(*args, **kwargs):
            raise NotFound("fixture serving relation missing")

        candidate.client.query = missing
    result = ask(candidate, BASELINE if broken == "corrupt_detail" else POINTS, "")
    assert result["status"] == (
        "publication_unavailable"
        if broken == "missing_relation"
        else "invalid_evidence"
    )
    assert not result["tables"]
    assert facts.calls == []


def test_uncovered_scope_or_undeclared_detail_uses_whole_fact_request(
    prepared, tmp_path
):
    evidence, publication = prepared
    snapshot = json.loads(publication.detail_json)
    summary_only = PilotPublication.build(
        snapshot,
        expected_snapshot_id=snapshot["sha256"],
        publication_id="points",
        relation_version="points/1",
        now=NOW,
    )
    facts = Facts(evidence)
    expected = ask(facts, BASELINE, "")
    facts.calls.clear()
    candidate = warehouse(tmp_path, facts, summary_only, "serve")
    assert (
        ask(candidate, BASELINE, "")["semantic_evidence"]
        == expected["semantic_evidence"]
    )
    assert facts.calls == [(SEASON,)]
    assert (
        len(candidate.client.calls) == 1
    )  # No detail read for an undeclared capability.

    facts.calls.clear()
    candidate.client.calls.clear()
    seasons = [SEASON, "2023-24"]
    _, actual = candidate.load_for_request(seasons, rich=True)
    assert actual is evidence
    assert facts.calls == [tuple(seasons)]
    assert candidate.client.calls == []


def test_uncovered_overview_baseline_keeps_facts_for_primary_retry(prepared, tmp_path):
    evidence, publication = prepared

    class PrimaryFacts(Facts):
        def load(self, seasons):
            result = super().load(seasons)
            if seasons != [SEASON]:
                raise SemanticError("unsupported_coverage", "Fixture covers one season")
            return result

    facts = PrimaryFacts(evidence)
    candidate = warehouse(tmp_path, facts, publication, "serve")
    answer = ask(
        candidate, "How did Regular Player perform in 2024-25 regular season?", ""
    )
    assert answer["status"] == "ok"
    assert facts.calls == [(SEASON, "2023-24"), (SEASON,)]
    assert candidate.client.calls == []


@pytest.mark.parametrize("override", [None, "off", "shadow", "serve"])
def test_production_composition_uses_effective_bundle_mode(monkeypatch, override):
    from scripts.serve_production_candidate import attach_publication

    if override is not None:
        monkeypatch.setenv("ASK_SERVING_MODE", override)
        monkeypatch.setenv("ASK_SERVING_REFERENCE_PATH", "reference.json")
    release = replace(
        source(ACTIVE),
        built_at=NOW.isoformat(),
        expires_at=(NOW + timedelta(days=1)).isoformat(),
    )
    monkeypatch.setattr(AskBundleWarehouse, "_utc_now", staticmethod(lambda: NOW))
    core = SimpleNamespace(
        workers=PublicationWorkers(),
        application=lambda _: SimpleNamespace(
            state=SimpleNamespace(ask_publication_source=lambda: release)
        ),
    )
    app = FastAPI()
    attach_publication(app, core, lambda: "production")
    config = Settings.from_env()
    repo = SimpleNamespace(
        settings=config,
        _governed_warehouse=Facts(release.evidence),
        ask_publication_source=app.state.ask_publication_source,
        ask_publication_serving_mode=app.state.ask_publication_serving_mode,
    )
    with TestClient(app), request_policy(config, "openai"):
        adapter = warehouse_for_repository(repo)
        assert adapter.publication_mode == (override or "serve")
        opened = adapter.open_publication()
        if adapter.publication_mode == "serve":
            assert opened.evidence is adapter.open_bundle().evidence
        else:
            assert opened is release
        assert opened.provenance == release.provenance
        adapter.recheck_publication(opened)


@pytest.mark.parametrize("suffix", ["", "/stream"])
def test_production_bundle_endpoint_parity_and_expiry(monkeypatch, suffix):
    now = datetime.fromisoformat(f"{LAST}T12:00:00+00:00") + timedelta(days=1)
    release = replace(
        source(ACTIVE),
        built_at=now.isoformat(),
        expires_at=(now + timedelta(days=1)).isoformat(),
    )
    monkeypatch.setattr(AskBundleWarehouse, "_utc_now", staticmethod(lambda: now))
    config = settings(DEFAULT_SEASON)
    facts = Facts(release.evidence)
    question = "Taylor Example average points"
    reference = ask(
        AskBundleWarehouse(facts, lambda _: release), question, suffix, config=config
    )
    adapter = AskBundleWarehouse(facts, lambda _: release, publication_mode="serve")
    served = ask(adapter, question, suffix, config=config)
    assert served["status"] == reference["status"] == "ok"
    for field in (
        "semantic_evidence",
        "claim_bindings",
        "source_coverage",
        "publication_id",
    ):
        assert served[field] == reference[field]
    assert served["publication_id"] == ACTIVE

    now += timedelta(days=1)
    refused = ask(adapter, question, suffix, config=config)
    assert refused["status"] == "invalid_evidence"
    assert not refused["tables"]
    assert facts.calls == []


def test_production_legacy_selection_keeps_historical_source():
    config = settings()
    adapter = AskBundleWarehouse(Facts(None), lambda _: None, publication_mode="serve")
    with request_policy(config, "openai"):
        assert adapter.open_publication() is None
        with pytest.raises(SemanticError) as error:
            adapter.open_publication(ACTIVE)
        assert error.value.code == "publication_unavailable"
