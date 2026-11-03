"""Phase 1 regression through real JSON/SSE handlers, no serving route enabled."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from tests.support.semantic import Warehouse, settings


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize("different", [False, True, "phase"])
def test_duplicate_rank_contract_at_endpoint(monkeypatch, suffix, different):
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository

    query = dict(
        metric="ast",
        aggregation="average",
        operation="rank",
        season="2024-25",
        season_type="Regular Season",
        min_games=1,
        limit=10,
    )
    plan = dict(status="compare", queries=[dict(query), dict(query)], message="")
    if different == "phase":
        plan["queries"][1]["season_type"] = "Playoffs"
    elif different:
        plan["queries"][1]["min_games"] = 20
    from tests.test_plan_execution import client_for

    model = client_for(plan)
    config = settings()
    repo = BigQueryWarehouseRepository(config, client=SimpleNamespace())
    repo._governed_warehouse = Warehouse()
    original = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: lambda: config,
            get_repository: lambda: repo,
            get_agent_client: lambda: model,
        }
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/agent/ask" + suffix + "?season=2024-25",
                json={
                    "question": "Top 10 assist leaders in both the regular season and the playoffs in 2024-25"
                    if different == "phase"
                    else "Top 10 players by assists per game in the 2024-25 regular season with at least 1 game"
                },
            )
        assert response.status_code == 200
        if suffix:
            events = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            payload = next(e["payload"] for e in events if e.get("type") == "final")
        else:
            payload = response.json()
        assert payload["status"] == ("invalid_plan" if different else "ok")
        assert bool(payload["semantic_evidence"]) is (not different)
        if not different:
            assert payload["semantic_plan"]["status"] == "query"
            assert len(payload["semantic_plan"]["queries"]) == 1
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original)


# Public orchestration tests use authored plans through the provider contract.
pytestmark = pytest.mark.usefixtures("scripted_intelligence")
