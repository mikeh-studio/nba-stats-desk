"""Synthetic provider content cannot become governed prose or saved history."""

import json
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.agent import claim_rollout
from app.agent import intelligence as host
from app.agent import intelligence_contract as api
from app.main import (
    AGENT_FAILED_DETAIL,
    app,
    get_agent_client,
    get_repository,
    get_settings,
)
from app.repository import BigQueryWarehouseRepository
from fastapi.testclient import TestClient
from tests.test_intelligence import FixtureProvider, plan
from tests.test_reference_intelligence import config, source

SENTINEL = "SYNTHETIC_PROVIDER_PROSE_MUST_NOT_ESCAPE"


@pytest.mark.parametrize(
    "outcome",
    ["query", "withheld", "fabricated", "exception", "typed_reason", "typed_metric"],
)
def test_governed_json_sse_history_and_observations_do_not_leak(
    monkeypatch, tmp_path, caplog, capfd, outcome
):
    class Provider(FixtureProvider):
        calls = 0

        def prompts(self):
            return api.PromptBundle("synthetic/1", SENTINEL)

        async def plan(self, context, intent, **kwargs):
            self.calls += 1
            assert kwargs["prompts"].planning_system == SENTINEL
            if outcome == "exception":
                raise ValueError(SENTINEL)
            if outcome == "fabricated":
                return {"answer": SENTINEL, "evidence": SENTINEL}
            if outcome == "withheld":
                return api.Withheld("clarification_required", "missing_metric")
            if outcome == "typed_reason":
                return api.Withheld("clarification_required", SENTINEL)
            if outcome == "typed_metric":
                return replace(
                    self.result,
                    queries=(replace(self.result.queries[0], metric=SENTINEL),),
                )
            return self.result

    provider = Provider(plan())
    host._load.cache_clear()
    monkeypatch.setattr(
        host,
        "entry_points",
        lambda **kw: [SimpleNamespace(load=lambda: lambda: provider)],
    )
    configured = replace(
        config(),
        ask_intelligence_provider="fixture",
        performance_cache_prewarm_enabled=False,
        agent_history_enabled=True,
        agent_history_path=str(tmp_path / "history.jsonl"),
    )
    repo = BigQueryWarehouseRepository(configured, client=SimpleNamespace())
    repo._governed_warehouse = source()
    model_calls = []

    def unexpected_model(**kwargs):
        model_calls.append(1)
        raise AssertionError("A governed fixture reached the legacy model transport")

    trap_client = SimpleNamespace(responses=SimpleNamespace(create=unexpected_model))
    previous = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: lambda: configured,
            get_repository: lambda: repo,
            get_agent_client: lambda: trap_client,
        }
    )
    claim_rollout.logger.addHandler(caplog.handler)  # This logger does not propagate.
    try:
        with (
            caplog.at_level(logging.INFO),
            TestClient(
                app, base_url="http://localhost", client=("127.0.0.1", 50000)
            ) as client,
        ):
            question = "Alex Example points per game in the 2024-25 regular season"
            responses = [
                client.post(endpoint + "?season=2024-25", json={"question": question})
                for endpoint in ("/api/agent/ask", "/api/agent/ask/stream")
            ]
            history = client.get("/api/agent/history?season=2024-25")
        assert provider.calls == 2
        assert model_calls == []
        assert history.status_code == 200
        assert responses[1].status_code == 200
        if outcome in ("query", "withheld"):
            assert responses[0].status_code == 200
            final = next(
                json.loads(line[6:])["payload"]
                for line in responses[1].text.splitlines()
                if line.startswith("data: ")
                and json.loads(line[6:]).get("type") == "final"
            )
            expected = "ok" if outcome == "query" else "clarification_required"
            assert responses[0].json()["status"] == final["status"] == expected
            assert responses[0].json()["answer"] == final["answer"]
            assert history.json()["conversations"]
            assert list(tmp_path.glob("*.jsonl"))
            assert "ask_claim_observation" in caplog.text
        else:
            assert responses[0].status_code == 502
            assert responses[0].json()["detail"] == AGENT_FAILED_DETAIL
            assert AGENT_FAILED_DETAIL in responses[1].text
            assert "invalid_provider_output" in caplog.text
        # Entire SSE wire includes intermediate events, not just the final payload.
        captured = capfd.readouterr()
        for text in [
            *(r.text for r in responses),
            history.text,
            caplog.text,
            captured.out,
            captured.err,
            *(p.read_text() for p in tmp_path.glob("*.jsonl")),
        ]:
            assert SENTINEL not in text
    finally:
        claim_rollout.logger.removeHandler(caplog.handler)
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        host._load.cache_clear()
