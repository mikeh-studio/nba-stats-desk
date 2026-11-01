"""Synthetic JSON/SSE regressions for leader qualification and requested seasons."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.agent.semantic_planner import (
    execute_plan,
    league_leader_plan,
    single_period_rank_plan,
)
from app.agent.semantic_source import (
    BigQuerySemanticSource,
    snapshot_digest,
    snapshot_evidence,
)
from app.agent.semantics import load_contract
from app.agent.serving_bigquery import BigQueryServingReader
from app.agent.serving_runtime import PilotPublication
from app.agent.serving_warehouse import ServingWarehouse
from app.main import app, get_agent_client, get_repository, get_settings
from app.repository import BigQueryWarehouseRepository
from fastapi.testclient import TestClient
from tests.semantic_intent_fixtures import migrate_legacy_plan
from tests.support.semantic import Warehouse, settings
from tests.test_first_try_defaults import rank
from tests.test_semantic_source import Client as SourceClient
from tests.test_semantic_source import source_rows
from tests.test_serving_bigquery import Client, wire
from tests.test_serving_runtime import NOW


class TwoSeasons:
    responses = property(lambda self: self)

    def create(self, **kwargs):
        plan = {
            "status": "compare",
            "message": "",
            "queries": [rank("2023-24"), rank("2024-25")],
        }
        return SimpleNamespace(
            output_text=json.dumps({"plan": migrate_legacy_plan(plan, kwargs)})
        )


def call(warehouse, question, suffix, model):
    config = replace(settings(), performance_cache_prewarm_enabled=False)
    repo = BigQueryWarehouseRepository(config, client=SimpleNamespace())
    repo._governed_warehouse = warehouse
    old = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: lambda: config,
            get_repository: lambda: repo,
            get_agent_client: lambda: model,
        }
    )
    try:
        with TestClient(app) as http:
            response = http.post(
                "/api/agent/ask" + suffix + "?season=2024-25",
                json={"question": question},
            )
        assert response.status_code == 200, response.text
        if not suffix:
            return response.json()
        return next(
            json.loads(line[6:])["payload"]
            for line in response.text.splitlines()
            if line.startswith("data: ") and json.loads(line[6:]).get("type") == "final"
        )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize("team_games,small_games,minimum", [(10, 5, 7), (4, 2, 3)])
def test_serving_leaders_match_fact_qualification(
    suffix, team_games, small_games, minimum
):
    originals = source_rows()
    rows = [r for r in originals if r["season_type"] == "Playoffs"]
    template = next(r for r in originals if r["season_type"] == "Regular Season")
    for pid, name, games, pts in [
        (1, "Regular Player", team_games, 10),
        (2, "Small Sample", small_games, 30),
    ]:
        for i in range(games):
            rows.append(
                dict(
                    template,
                    player_id=pid,
                    player_name=name,
                    game_id=f"g{i:02}",
                    game_date=f"2025-01-{i + 1:02}",
                    pts=pts,
                    home_away="HOME",
                )
            )
    snap = BigQuerySemanticSource(SourceClient(rows), project="test-project").capture(
        ["2024-25"]
    )
    snap["source_timestamp"] = NOW.isoformat()
    snap["sha256"] = snapshot_digest(snap)
    evidence = snapshot_evidence(snap)
    pub = PilotPublication.build(
        snap,
        expected_snapshot_id=snap["sha256"],
        publication_id="review",
        relation_version="expanded/1",
        now=NOW,
        metrics=list(load_contract().metrics),
        include_detail=True,
    )
    ref, records = wire(pub)
    facts = SimpleNamespace(load=lambda seasons: ({"capture": {}}, evidence))

    def forbidden(*args):
        raise AssertionError(
            "Serving leaders must use publication appearances, not facts"
        )

    client = Client(records)
    serving = ServingWarehouse(
        SimpleNamespace(load=forbidden),
        BigQueryServingReader(client, ref),
        lambda: NOW,
        mode="serve",
        families=("points", "rankings"),
    )
    question = "Who led the league in scoring in 2024-25?"
    actual = [call(w, question, suffix, object()) for w in [facts, serving]]
    for payload in actual:
        assert payload["status"] == "ok", payload["answer"]
    assert actual[0]["semantic_evidence"]["cohort"]["min_games"] == minimum
    assert actual[1]["semantic_evidence"]["cohort"]["min_games"] == minimum
    assert [a["semantic_evidence"]["rows"][0]["player_id"] for a in actual] == [1, 1]
    assert (
        actual[0]["semantic_evidence"]["rows"] == actual[1]["semantic_evidence"]["rows"]
    )
    assert len(client.calls) == 1  # Core publication only; no detail preload.


@pytest.mark.parametrize("suffix", ["", "/stream"])
def test_explicit_two_seasons_are_not_silently_discarded(suffix):
    payload = call(
        Warehouse(),
        "Who led the league in scoring in 2023-24 and 2024-25?",
        suffix,
        TwoSeasons(),
    )
    assert payload["status"] == "invalid_plan", payload["answer"]
    assert not payload.get("semantic_evidence")
    assert not payload.get("tables")
    assert "no numbers are shown" in payload["answer"]


@pytest.mark.parametrize(
    "periods",
    ["2023-24 and 2024-25", "2023/24 and 2024–25", "2023-2024, 2024-2025", "2023-24"],
)
def test_rank_repair_requires_only_the_selected_season(periods):
    plan = {"status": "compare", "queries": [rank("2023-24"), rank("2024-25")]}
    assert (
        single_period_rank_plan(
            plan, f"Who led the league in scoring in {periods}?", "2024-25"
        )
        is plan
    )


def test_leader_without_qualification_evidence_fails_closed():
    def forbidden(query):
        raise AssertionError("An unqualified leaderboard must not execute")

    result = execute_plan(
        league_leader_plan("Who led the league in scoring in 2024-25?", "2024-25"),
        None,
        [],
        query_runner=forbidden,
    )
    assert result["status"] == "invalid_evidence"
    assert result["evidence"] is None


# Public orchestration tests use authored plans through the provider contract.
pytestmark = pytest.mark.usefixtures("scripted_intelligence")
