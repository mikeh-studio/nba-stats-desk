from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import (
    PAYLOAD_CACHE_MAX_ENTRIES,
    STATIC_VERSION,
    _get_cached_payload,
    _health_cache,
    _payload_building,
    _payload_cache,
    _payload_cache_lock,
    _payload_refreshing,
    _performance_initial_cache_key,
    _player_detail_cache_key,
    _prewarm_performance_flow,
    _store_payload_locked,
    app,
)
from app.seasons import DEFAULT_SEASON
from app.telemetry import LOGGER_NAME
from tests.support.api import (
    FakeOpenAIClient,
    FakeRepository,
    _test_settings,
    build_client,
)


class StaleRepository(FakeRepository):
    def get_health(self) -> dict:
        return {
            "season": "2025-26",
            "status": "stale",
            "is_fresh": False,
            "checked_at_utc": "2026-02-11T12:00:00+00:00",
            "age_hours": 48.0,
            "threshold_hours": 36,
            "last_successful_finished_at_utc": "2026-02-09T12:00:00+00:00",
        }


class HealthExplodingRepository(FakeRepository):
    def get_health(self) -> dict:
        raise AssertionError("page shell should not synchronously load health")


class CountingPerformanceRepository(FakeRepository):
    def __init__(self) -> None:
        self.health_calls = 0
        self.initial_calls = 0
        self.date_calls = 0
        self.game_calls = 0
        self.player_calls = 0

    def get_health(self) -> dict:
        self.health_calls += 1
        return super().get_health()

    def get_recent_performance_initial(
        self,
        *,
        game_date: str | None = None,
        game_id: str | None = None,
        limit: int = 240,
    ) -> dict:
        self.initial_calls += 1
        dates = FakeRepository.get_recent_performance_dates(self)
        selected_date = game_date or (dates[0]["value"] if dates else None)
        if not selected_date:
            return {
                "season": "2025-26",
                "dates": dates,
                "selected_date": None,
                "selected_game_id": game_id,
                "games": [],
                "players": [],
            }
        return {
            "season": "2025-26",
            "dates": dates,
            "selected_date": selected_date,
            "selected_game_id": game_id,
            "games": FakeRepository.get_recent_performance_games(
                self,
                game_date=selected_date,
            ),
            "players": FakeRepository.get_recent_performance_players(
                self,
                game_date=selected_date,
                game_id=game_id,
                limit=limit,
            ),
        }

    def get_recent_performance_dates(self) -> list[dict]:
        self.date_calls += 1
        return super().get_recent_performance_dates()

    def get_recent_performance_games(
        self, *, game_date: str | None = None
    ) -> list[dict]:
        self.game_calls += 1
        return super().get_recent_performance_games(game_date=game_date)

    def get_recent_performance_players(
        self,
        *,
        game_date: str,
        game_id: str | None = None,
        limit: int = 240,
    ) -> list[dict]:
        self.player_calls += 1
        return super().get_recent_performance_players(
            game_date=game_date,
            game_id=game_id,
            limit=limit,
        )


class CountingPlayerDetailRepository(FakeRepository):
    def __init__(self) -> None:
        self.detail_calls: list[int] = []

    def get_player_detail(self, player_id: int) -> dict | None:
        self.detail_calls.append(player_id)
        return super().get_player_detail(player_id)


class ExplodingPrewarmRepository(FakeRepository):
    def get_health(self) -> dict:
        raise RuntimeError("health unavailable")

    def get_recent_performance_initial(
        self,
        *,
        game_date: str | None = None,
        game_id: str | None = None,
        limit: int = 240,
    ) -> dict:
        raise RuntimeError("initial payload unavailable")


class MissingOpportunityRepository(FakeRepository):
    def get_dashboard(self, as_of_date: str | None = None) -> dict:
        payload = super().get_dashboard(as_of_date=as_of_date)
        payload["opportunity"] = []
        return payload

    def get_player_detail(self, player_id: int) -> dict | None:
        detail = super().get_player_detail(player_id)
        if detail is None:
            return None
        if player_id == 7:
            detail["panel_states"]["opportunity"] = "unavailable"
            detail["opportunity"] = None
        return detail


class FakeOpenAIDateRangeResponses:
    def __init__(self) -> None:
        self.calls = 0
        self.first_tools: list[dict] = []

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            self.first_tools = kwargs["tools"]
            return SimpleNamespace(
                output=[
                    SimpleNamespace(
                        type="function_call",
                        name="get_player_game_log",
                        arguments=json.dumps(
                            {
                                "player_id": 7,
                                "metrics": ["points"],
                                "limit": 82,
                                "start_date": "2026-02-02",
                                "end_date": "2026-02-03",
                            }
                        ),
                        call_id="call_1",
                    )
                ],
                output_text="",
            )
        return SimpleNamespace(
            output=[],
            output_text=json.dumps(
                {
                    "answer": "Date range filtered game log returned.",
                    "assumptions": [],
                    "tables": [],
                    "charts": [],
                    "metric_definitions": [],
                    "followups": [],
                }
            ),
        )


class FakeOpenAIDateRangeClient:
    def __init__(self) -> None:
        self.responses = FakeOpenAIDateRangeResponses()


def test_home_redirects_to_ask() -> None:
    client = build_client()
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/ask"


def test_analysis_page_removed() -> None:
    client = build_client()
    response = client.get("/analysis")

    assert response.status_code == 404


def test_recommendations_page_removed() -> None:
    client = build_client()
    response = client.get("/recommendations")

    assert response.status_code == 404


def test_ask_page_smoke() -> None:
    client = build_client()
    response = client.get("/ask")

    assert response.status_code == 200
    assert "Ask NBA Stats" in response.text
    assert f"/static/agent.js?v={STATIC_VERSION}" in response.text
    assert (
        f"ask_comparison.js?v={STATIC_VERSION}"
        in (Path(__file__).parents[1] / "app/static/agent.js").read_text()
    )
    assert (
        f"ask_presentation.js?v={STATIC_VERSION}"
        in (Path(__file__).parents[1] / "app/static/agent.js").read_text()
    )
    for module in (
        "agent.js",
        "ask_presentation.js",
        "ask_comparison.js",
        "metric_drawer.js",
    ):
        assert (
            f"ask_claim_bindings.js?v={STATIC_VERSION}"
            in (Path(__file__).parents[1] / "app/static" / module).read_text()
        )
    assert "data-health-status" in response.text
    assert "data-agent-provider" in response.text
    assert "data-agent-model" in response.text
    assert "data-chat-tabs" in response.text
    assert "data-chat-more" in response.text
    assert "data-followup-form" in response.text
    assert "data-agent-history-list" in response.text
    assert "data-agent-new-chat" in response.text
    assert "data-history-search" in response.text
    # Each answer owns its profile and evidence; the timeline is mounted here.
    assert "data-agent-answer" in response.text
    assert f"/static/ask.css?v={STATIC_VERSION}" in response.text
    assert "gpt-6-astra" in response.text
    assert "gpt-5.6-sol" in response.text
    assert "gpt-5.6-terra" in response.text
    assert "gpt-5.6-luna" in response.text
    assert "claude-fable-5-1" in response.text
    assert "claude-opus-5" in response.text
    assert "claude-sonnet-5" in response.text
    assert "Claude (Anthropic)" in response.text
    assert ">ASK</a>" in response.text
    assert ">PLAYERS</a>" not in response.text
    assert ">TRENDING</a>" in response.text
    assert ">PERFORMANCE</a>" in response.text
    assert ">PROFILES</a>" in response.text
    assert ">COMPARE</a>" not in response.text
    assert ">RESEARCH</a>" not in response.text
    assert ">Dashboard</a>" not in response.text
    assert ">Visualize</a>" not in response.text


def test_ask_page_does_not_block_on_health() -> None:
    client = build_client(repo=HealthExplodingRepository())
    response = client.get("/ask")

    assert response.status_code == 200
    assert "Data status loading" not in response.text


def test_api_agent_ask_returns_disabled_without_openai_key() -> None:
    client = build_client(settings=_test_settings(openai_api_key=None))
    response = client.post(
        "/api/agent/ask",
        json={"question": "How is Tyrese Maxey trending?"},
    )

    assert response.status_code == 503
    assert response.headers["x-request-id"]
    assert response.json()["detail"] == "Ask NBA Stats is unavailable."


def test_api_agent_ask_vague_question_requires_openai_key() -> None:
    client = build_client(settings=_test_settings(openai_api_key=None))
    response = client.post(
        "/api/agent/ask",
        json={"question": "stats"},
    )

    assert response.status_code == 503
    assert response.headers["x-request-id"]
    assert response.json()["detail"] == "Ask NBA Stats is unavailable."


def test_api_agent_ask_runs_mocked_openai_tool_loop() -> None:
    fake_openai = FakeOpenAIClient()
    client = build_client(
        settings=_test_settings(openai_api_key="test-key"),
        agent_client=fake_openai,
    )
    response = client.post(
        "/api/agent/ask",
        json={"question": "How is Tyrese Maxey trending?"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["season"] == DEFAULT_SEASON
    assert payload["request_id"]
    assert response.headers["x-request-id"] == payload["request_id"]
    assert payload["answer"].startswith("Tyrese Maxey")
    assert payload["player_profile"]["player"]["player_id"] == 7
    assert payload["player_profile"]["player"]["player_name"] == "Tyrese Maxey"
    assert payload["player_profile"]["profile_url"] == "/players/7"
    assert payload["player_profile"]["recent_form"][0]["window_key"] == "last_5"
    assert payload["player_profile"]["stat_percentiles"][0]["label"] == "PTS"
    assert payload["conversation_id"].startswith("agent-")
    assert payload["tool_calls"][0]["name"] == "resolve_player"
    assert payload["tool_calls"][0]["status"] == "ok"
    assert payload["tool_calls"][0]["args"] == {"name": "Tyrese Maxey", "limit": 5}
    assert isinstance(payload["tool_calls"][0]["duration_ms"], int)
    assert payload["tool_calls"][0]["result"]["status"] == "ok"
    assert payload["agent_plan"]["route"] == "player_trend"
    assert payload["agent_plan"]["confidence"] > 0
    assert payload["agent_plan"]["required_tools"] == [
        "resolve_player",
        "get_player_trends",
    ]
    assert payload["player_profile"]["player"]["headshot_url"].endswith("/7.png")
    assert payload["player_profile"]["player"]["overall_rank"] == 12
    developer_messages = [
        message
        for message in fake_openai.responses.kwargs[-1]["input"]
        if message.get("role") == "developer"
    ]
    assert any(
        "Routing plan for this question" in message.get("content", "")
        for message in developer_messages
    )
    assert fake_openai.responses.calls == 2


def test_api_agent_ask_writes_local_history(tmp_path: Path) -> None:
    history_path = tmp_path / "ask_history.jsonl"
    client = build_client(
        settings=_test_settings(
            openai_api_key="test-key",
            agent_history_enabled=True,
            agent_history_path=str(history_path),
        ),
        agent_client=FakeOpenAIClient(),
    )

    response = client.post(
        "/api/agent/ask",
        json={"question": "How is Tyrese Maxey trending?"},
        headers={"X-Request-ID": "req-history-json"},
    )

    assert response.status_code == 200
    payload = response.json()
    records = [json.loads(line) for line in history_path.read_text().splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record["conversation_id"] == payload["conversation_id"]
    assert record["request_id"] == response.headers["x-request-id"]
    assert record["question"] == "How is Tyrese Maxey trending?"
    assert record["provider"] == "openai"
    assert record["model"] == "gpt-5.4-mini"
    assert record["payload"]["request_id"] == response.headers["x-request-id"]
    assert record["payload"]["answer"] == payload["answer"]


def test_api_agent_history_is_public_safe_by_default(tmp_path: Path) -> None:
    history_path = tmp_path / "ask_history_disabled.jsonl"
    client = build_client(
        settings=_test_settings(
            openai_api_key="test-key",
            agent_history_path=str(history_path),
        ),
        agent_client=FakeOpenAIClient(),
    )

    response = client.post(
        "/api/agent/ask",
        json={"question": "How is Tyrese Maxey trending?"},
        headers={"X-Request-ID": "req-history-disabled"},
    )

    assert response.status_code == 200
    assert not history_path.exists()
    assert client.get("/api/agent/history").json() == {"conversations": []}
    assert client.delete("/api/agent/history").json() == {"status": "ok"}


def test_api_agent_history_groups_full_payloads_and_clears(tmp_path: Path) -> None:
    history_path = tmp_path / "ask_history.jsonl"
    client = build_client(
        settings=_test_settings(
            openai_api_key="test-key",
            agent_history_enabled=True,
            agent_history_path=str(history_path),
        ),
        agent_client=FakeOpenAIClient(),
    )

    first = client.post(
        "/api/agent/ask",
        json={"question": "How is Tyrese Maxey trending?"},
        headers={"X-Request-ID": "req-history-1"},
    )
    conversation_id = first.json()["conversation_id"]
    second = client.post(
        "/api/agent/ask",
        json={
            "question": "What changed in the last 5 games?",
            "conversation_id": conversation_id,
        },
        headers={"X-Request-ID": "req-history-2"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    history = client.get("/api/agent/history?limit=25")
    assert history.status_code == 200
    conversations = history.json()["conversations"]
    assert len(conversations) == 1
    assert conversations[0]["conversation_id"] == conversation_id
    assert [turn["request_id"] for turn in conversations[0]["turns"]] == [
        first.headers["x-request-id"],
        second.headers["x-request-id"],
    ]
    assert conversations[0]["turns"][0]["payload"]["answer"].startswith("Tyrese Maxey")

    cleared = client.delete("/api/agent/history")
    assert cleared.status_code == 200
    assert cleared.json() == {"status": "ok"}
    assert client.get("/api/agent/history").json() == {"conversations": []}


def test_api_agent_ask_uses_selected_openai_model() -> None:
    fake_openai = FakeOpenAIClient()
    client = build_client(
        settings=_test_settings(openai_api_key="test-key"),
        agent_client=fake_openai,
    )
    response = client.post(
        "/api/agent/ask",
        json={"question": "How is Tyrese Maxey trending?", "model": "gpt-6-astra"},
    )

    assert response.status_code == 200
    assert fake_openai.responses.calls == 2
    assert all(call["model"] == "gpt-6-astra" for call in fake_openai.responses.kwargs)


def test_saved_history_rehydrates_followup_context_after_restart(tmp_path, monkeypatch):
    from app import main as main_module
    from app.agent.conversation import InMemoryConversationStore
    from app.agent.history import append_history_turn

    history_path = tmp_path / "saved.jsonl"
    append_history_turn(
        history_path,
        conversation_id="saved-chat",
        request_id="saved-turn",
        question="Jalen Johnson performance past 12 months",
        provider="test",
        model="fixture",
        payload={
            "status": "ok",
            "answer": "Saved answer",
            "player_profile": {
                "player": {"player_id": 1, "player_name": "Jalen Johnson"}
            },
            "semantic_evidence": {
                "metrics": [1],
                "scope": {
                    "start": "2025-09-13",
                    "end": "2026-09-12",
                    "phases": ["Regular Season"],
                },
            },
        },
    )
    store = InMemoryConversationStore()
    # Many clarification turns must not push the successful context out of recovery.
    for index in range(8):
        append_history_turn(
            history_path,
            conversation_id="saved-chat",
            request_id=f"clarify-{index}",
            question="Which metric?",
            provider="test",
            model="fixture",
            payload={"status": "clarification_required", "answer": "Please clarify"},
        )
    monkeypatch.setattr(main_module, "_season_conversation_store", lambda season: store)
    client = build_client(
        settings=_test_settings(
            openai_api_key="test-key",
            agent_history_enabled=True,
            agent_history_path=str(history_path),
        ),
        agent_client=FakeOpenAIClient(),
    )
    result = client.post(
        "/api/agent/ask",
        json={"question": "How about his assists?", "conversation_id": "saved-chat"},
    )
    assert result.status_code == 200
    turns = store.get_turns("saved-chat", max_turns=10)
    assert turns[0].context["players"] == [
        {"player_id": 1, "player_name": "Jalen Johnson"}
    ]
    assert (
        turns[0].question
        == "Jalen Johnson performance from 2025-09-13 through 2026-09-12 (Regular Season)"
    )
    assert (
        client.get("/api/agent/history?q=Jalen&limit=1").json()["conversations"][0][
            "conversation_id"
        ]
        == "saved-chat"
    )
    assert (
        client.get("/api/agent/history?conversation_id=absent").json()["conversations"]
        == []
    )


def test_browser_only_context_recovers_after_contextless_clarification(monkeypatch):
    from app import main as main_module
    from app.agent.conversation import InMemoryConversationStore
    from app.agent.followup import resolve_followup

    store = InMemoryConversationStore()
    store.set_pending_clarification(
        "browser-chat", question="Which Johnson?", query_plan=None
    )
    monkeypatch.setattr(main_module, "_season_conversation_store", lambda season: store)
    client = build_client(
        settings=_test_settings(openai_api_key="test-key", agent_history_enabled=False),
        agent_client=FakeOpenAIClient(),
    )
    prior = {
        "question": "Jalen Johnson performance past 12 months",
        "players": [{"player_id": 1, "player_name": "Jalen Johnson"}],
        "scope": {"start": "2025-09-13", "end": "2026-09-12"},
        "metrics": ["ast"],
    }
    question = "Beside Johnson, who are the other the other top playmaking leads"
    response = client.post(
        "/api/agent/ask",
        json={
            "question": question,
            "conversation_id": "browser-chat",
            "previous_context": prior,
        },
    )
    assert response.status_code == 200
    recovered = store.get_turns("browser-chat", max_turns=10)[0].context
    assert recovered["browser_recovered"] is True
    resolved, hints = resolve_followup(question, recovered)
    assert "Beside Jalen Johnson" in resolved
    assert "from 2025-09-13 through 2026-09-12" in resolved
    assert hints["excluded_player_ids"] == [1]
    assert "assists per game" in resolved
    prior["players"][0]["player_name"] = "Different Player"
    client.post(
        "/api/agent/ask",
        json={
            "question": question,
            "conversation_id": "browser-chat",
            "previous_context": prior,
        },
    )
    assert store.get_turns("browser-chat", max_turns=10)[0].context == recovered


def test_api_agent_ask_rejects_model_for_wrong_provider(caplog) -> None:
    client = build_client(settings=_test_settings(openai_api_key="test-key"))
    with caplog.at_level("INFO", logger="app.agent"):
        response = client.post(
            "/api/agent/ask",
            json={
                "question": "How is Tyrese Maxey trending?",
                "provider": "openai",
                "model": "claude-fable-5",
            },
        )

    assert response.status_code == 400
    assert response.headers["x-request-id"]
    assert response.json()["detail"] == "Unsupported openai model: claude-fable-5"
    summaries = [
        json.loads(record.message)
        for record in caplog.records
        if "agent_request_summary" in record.message
    ]
    assert summaries[-1]["request_id"] == response.headers["x-request-id"]
    assert summaries[-1]["model"] == "unvalidated"
    assert summaries[-1]["outcome"] == "error"
    assert summaries[-1]["error_type"] == "HTTP_400"


def test_api_agent_ask_accepts_date_range_tool_args() -> None:
    fake_openai = FakeOpenAIDateRangeClient()
    client = build_client(
        settings=_test_settings(openai_api_key="test-key"),
        agent_client=fake_openai,
    )
    response = client.post(
        "/api/agent/ask",
        json={"question": "Show Tyrese Maxey points from 2026-02-02 to 2026-02-03."},
    )

    assert response.status_code == 200
    payload = response.json()
    game_log_call = next(
        tool for tool in payload["tool_calls"] if tool["name"] == "get_player_game_log"
    )
    assert game_log_call["status"] == "ok"
    assert game_log_call["args"]["start_date"] == "2026-02-02"
    assert game_log_call["args"]["end_date"] == "2026-02-03"


def test_api_agent_ask_rate_limits_repeated_public_calls() -> None:
    fake_openai = FakeOpenAIClient()
    client = build_client(
        settings=_test_settings(
            openai_api_key="test-key",
            agent_rate_limit_per_minute=1,
        ),
        agent_client=fake_openai,
    )
    client = TestClient(app, client=("198.51.100.23", 50000))
    headers = {"X-Forwarded-For": "198.51.100.23"}

    first = client.post(
        "/api/agent/ask",
        json={"question": "How is Tyrese Maxey trending?"},
        headers=headers,
    )
    second = client.post(
        "/api/agent/ask",
        json={"question": "How is Tyrese Maxey trending?"},
        headers=headers,
    )

    assert first.status_code == 200
    assert second.status_code == 429
    assert "rate limit" in second.json()["detail"]
    assert fake_openai.responses.calls == 2


def test_api_agent_ask_rate_limit_ignores_spoofed_forwarded_prefix() -> None:
    fake_openai = FakeOpenAIClient()
    client = build_client(
        settings=_test_settings(
            openai_api_key="test-key",
            agent_rate_limit_per_minute=1,
            agent_trusted_proxy_cidrs=("10.0.0.0/24",),
        ),
        agent_client=fake_openai,
    )

    client = TestClient(app, client=("10.0.0.5", 50000))
    first = client.post(
        "/api/agent/ask",
        json={"question": "How is Tyrese Maxey trending?"},
        headers={"X-Forwarded-For": "10.0.0.1, 198.51.100.30"},
    )
    second = client.post(
        "/api/agent/ask",
        json={"question": "How is Tyrese Maxey trending?"},
        headers={"X-Forwarded-For": "10.0.0.2, 198.51.100.30"},
    )

    assert first.status_code == 200
    # Only the right-most (proxy-appended) hop counts, so rotating the
    # client-supplied prefix must not reset the limit.
    assert second.status_code == 429


def test_payload_cache_evicts_oldest_entries_past_cap() -> None:
    with _payload_cache_lock:
        saved = dict(_payload_cache)
        _payload_cache.clear()
        try:
            for index in range(PAYLOAD_CACHE_MAX_ENTRIES + 25):
                _store_payload_locked(("bound-test", index), {"index": index})
            assert len(_payload_cache) == PAYLOAD_CACHE_MAX_ENTRIES
            assert ("bound-test", 0) not in _payload_cache
            newest = ("bound-test", PAYLOAD_CACHE_MAX_ENTRIES + 24)
            assert newest in _payload_cache
        finally:
            _payload_cache.clear()
            _payload_cache.update(saved)


def test_api_agent_ask_rejects_overlong_question() -> None:
    client = build_client(
        settings=_test_settings(
            openai_api_key="test-key",
            agent_question_max_chars=12,
        ),
        agent_client=FakeOpenAIClient(),
    )

    response = client.post(
        "/api/agent/ask",
        json={"question": "How is Tyrese Maxey trending?"},
    )

    assert response.status_code == 400
    assert "too long" in response.json()["detail"]


def test_api_agent_ask_enforces_daily_rate_limit() -> None:
    fake_openai = FakeOpenAIClient()
    client = build_client(
        settings=_test_settings(
            openai_api_key="test-key",
            agent_rate_limit_per_minute=0,
            agent_rate_limit_daily=1,
        ),
        agent_client=fake_openai,
    )
    client = TestClient(app, client=("198.51.100.24", 50000))
    headers = {"X-Forwarded-For": "198.51.100.24"}

    first = client.post(
        "/api/agent/ask",
        json={"question": "How is Tyrese Maxey trending?"},
        headers=headers,
    )
    second = client.post(
        "/api/agent/ask",
        json={"question": "How is Tyrese Maxey trending?"},
        headers=headers,
    )

    assert first.status_code == 200
    assert second.status_code == 429
    assert "daily" in second.json()["detail"]


def test_api_agent_ask_logs_structured_summary(caplog) -> None:
    fake_openai = FakeOpenAIClient()
    client = build_client(
        settings=_test_settings(openai_api_key="test-key"),
        agent_client=fake_openai,
    )

    with caplog.at_level("INFO", logger="app.agent"):
        response = client.post(
            "/api/agent/ask",
            json={"question": "How is Tyrese Maxey trending?"},
            headers={"X-Request-ID": "req-test-1"},
        )

    assert response.status_code == 200
    summaries = [
        json.loads(record.message)
        for record in caplog.records
        if "agent_request_summary" in record.message
    ]
    assert summaries[-1]["request_id"] == response.headers["x-request-id"]
    assert response.headers["x-request-id"] != "req-test-1"
    assert summaries[-1]["route"] == "player_trend"
    assert summaries[-1]["outcome"] == "answered"
    assert summaries[-1]["total_tool_calls"] > 0
    assert "tools" not in summaries[-1]
    assert "question" not in summaries[-1]
    assert "conversation_id" not in summaries[-1]


def test_api_agent_ask_stream_sends_progress_and_final_payload() -> None:
    fake_openai = FakeOpenAIClient()
    client = build_client(
        settings=_test_settings(openai_api_key="test-key"),
        agent_client=fake_openai,
    )

    response = client.post(
        "/api/agent/ask/stream",
        json={"question": "How is Tyrese Maxey trending?"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: meta" in response.text
    assert "event: plan" in response.text
    assert "event: tool_start" in response.text
    assert "event: answer_delta" in response.text
    assert "event: final" in response.text
    assert "Tyrese Maxey resolves" in response.text
    assert '"player_profile":' in response.text
    assert '"profile_url": "/players/7"' in response.text


def test_api_agent_ask_stream_preserves_markdown_deltas_and_logs_history(
    tmp_path: Path,
) -> None:
    markdown_answer = "## Summary\n\n### Context\n\n- **PTS:** 30\n- `AST`: 7"
    history_path = tmp_path / "stream_history.jsonl"
    client = build_client(
        settings=_test_settings(
            openai_api_key="test-key",
            agent_history_enabled=True,
            agent_history_path=str(history_path),
        ),
        agent_client=FakeOpenAIClient(answer=markdown_answer),
    )

    response = client.post(
        "/api/agent/ask/stream",
        json={"question": "Summarize Tyrese Maxey in Markdown."},
        headers={"X-Request-ID": "req-history-stream"},
    )

    assert response.status_code == 200
    assert "event: answer_delta" in response.text
    assert "## Summary\\n\\n### Context" in response.text
    records = [json.loads(line) for line in history_path.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["request_id"] == response.headers["x-request-id"]
    assert records[0]["payload"]["answer"] == markdown_answer


def test_api_agent_ask_stream_rejects_overlong_question_with_request_id() -> None:
    client = build_client(
        settings=_test_settings(
            openai_api_key="test-key",
            agent_question_max_chars=12,
        ),
        agent_client=FakeOpenAIClient(),
    )

    response = client.post(
        "/api/agent/ask/stream",
        json={"question": "How is Tyrese Maxey trending?"},
    )

    assert response.status_code == 400
    assert response.headers["x-request-id"]
    assert "too long" in response.json()["detail"]


def test_player_page_smoke() -> None:
    client = build_client()
    response = client.get("/players/7/content")

    assert response.status_code == 200
    assert "Why This Player Matters" in response.text
    assert "Archetype" in response.text
    assert "<h2>Statistical Profile Similarity</h2>" in response.text
    assert "Gaussian mixture" in response.text
    assert "Compare player" in response.text
    assert "Find qualified player" in response.text
    assert "Performance" in response.text
    assert "Game Log" in response.text
    assert "Recent Windows" in response.text
    assert "League Percentiles" in response.text
    assert "P-Rating 91.2" in response.text
    assert "Ball Security" in response.text
    assert "Box Score Index" in response.text
    assert "Primary Creator" in response.text
    assert "PHI vs. NYK" in response.text
    assert "Jalen Brunson" in response.text
    assert "Loading game log." not in response.text
    assert "data-player-detail-hydrate" not in response.text
    assert "https://cdn.nba.com/headshots/nba/latest/1040x760/7.png" in response.text


def test_player_page_unavailable_state_smoke() -> None:
    client = build_client()
    response = client.get("/players/9/content")

    assert response.status_code == 200
    assert "This player is not currently ranked." in response.text


def test_player_page_does_not_block_on_health() -> None:
    client = build_client(repo=HealthExplodingRepository())
    response = client.get("/players/7/content")

    assert response.status_code == 200
    assert "Jalen Brunson" in response.text


def test_compare_page_with_only_player_a() -> None:
    client = build_client()
    response = client.get("/compare?player_a_id=7")

    assert response.status_code == 200
    assert "Build Comparison" in response.text
    assert "Pick a second player to finish the comparison." in response.text
    assert "Regular Season" in response.text
    assert "First Half Regular Season" in response.text
    assert "Second Half Regular Season" in response.text
    assert "Playoffs" in response.text


def test_compare_page_full_surface() -> None:
    client = build_client()
    response = client.get(
        "/compare?player_a_id=7&player_b_id=9&window=last_7&focus=scoring"
    )

    assert response.status_code == 200
    assert "Comparison Surface" in response.text
    assert "Focus Scoring" in response.text
    assert "Mikal Bridges" in response.text
    assert "Limited comparison data." in response.text
    assert "Statistical Profile Similarity." in response.text
    assert "Box Score Index" in response.text
    assert "Last 7" in response.text
    assert "https://cdn.nba.com/headshots/nba/latest/1040x760/7.png" in response.text


def test_compare_page_supports_regular_season_window() -> None:
    client = build_client()
    response = client.get("/compare?player_a_id=7&player_b_id=9&window=regular_season")

    assert response.status_code == 200
    assert "Window Regular Season" in response.text
    assert "82 games" in response.text
    assert "82/None games" not in response.text


def test_compare_page_duplicate_validation() -> None:
    client = build_client()
    response = client.get("/compare?player_a_id=7&player_b_id=7")

    assert response.status_code == 200
    assert "Compare players must be different." in response.text


def test_api_health_smoke() -> None:
    client = build_client()
    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "fresh"
    assert payload["last_successful_finished_at_utc"] == "2026-02-11T01:15:00+00:00"
    assert payload["season_coverage"]["is_full_season"] is True
    assert "latest_successful_run" not in payload


def test_api_player_search_rejects_blank_query() -> None:
    client = build_client()
    response = client.get("/api/players/search?q=%20%20%20")

    assert response.status_code == 400
    assert response.json()["detail"] == "Search query must not be blank"


def test_api_player_search_returns_qualified_player_metadata() -> None:
    client = build_client()
    response = client.get("/api/players/search?q=maxey")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["player_name"] == "Tyrese Maxey"
    assert item["is_qualified"] is True
    assert item["games_sampled"] == 12
    assert item["headshot_url"].endswith("/7.png")


def test_api_player_detail_available_player() -> None:
    client = build_client()
    response = client.get("/api/players/7")

    assert response.status_code == 200
    payload = response.json()
    assert payload["item"]["availability_state"] == "fresh"
    assert payload["item"]["player"]["overall_rank"] == 12
    assert payload["item"]["sample"]["games_sampled"] == 12
    assert payload["item"]["stat_percentiles"][0]["label"] == "PTS"
    assert payload["item"]["chart_baselines"]["pts"]["value"] == 12.4
    assert payload["item"]["game_log"]["games_returned"] == 5
    assert payload["item"]["player"]["headshot_url"].endswith("/7.png")
    assert payload["item"]["archetype"]["archetype_label"] == "Primary Creator"
    assert payload["item"]["similar_players"][0]["player_name"] == "Jalen Brunson"


def test_player_detail_cache_reuses_full_payload_for_page_and_api() -> None:
    repo = CountingPlayerDetailRepository()
    key = _player_detail_cache_key(repo, 7)
    with _payload_cache_lock:
        _payload_cache.pop(key, None)
        _payload_refreshing.discard(key)
        building = _payload_building.pop(key, None)
    if building is not None:
        building.set()

    try:
        client = build_client(repo)

        first_page = client.get("/players/7/content")
        second_page = client.get("/players/7/content")
        first_api = client.get("/api/players/7")
        second_api = client.get("/api/players/7")

        assert first_page.status_code == 200
        assert second_page.status_code == 200
        assert first_api.status_code == 200
        assert second_api.status_code == 200
        assert repo.detail_calls == [7]
        assert "Jalen Brunson" in first_page.text
        assert (
            first_api.json()["item"]["similar_players"][0]["player_name"]
            == "Jalen Brunson"
        )
    finally:
        with _payload_cache_lock:
            _payload_cache.pop(key, None)
            _payload_refreshing.discard(key)
            building = _payload_building.pop(key, None)
        if building is not None:
            building.set()


def test_api_player_detail_unavailable_known_player() -> None:
    client = build_client()
    response = client.get("/api/players/9")

    assert response.status_code == 200
    payload = response.json()
    assert payload["item"]["availability_state"] == "unavailable"
    assert payload["item"]["availability_reason"] == "Not currently ranked"


def test_api_player_detail_404() -> None:
    client = build_client()
    response = client.get("/api/players/999")

    assert response.status_code == 404


def test_api_compare_smoke() -> None:
    client = build_client()
    response = client.get(
        "/api/compare?player_a_id=7&player_b_id=9&window=last_7&focus=scoring"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["window"] == "last_7"
    assert payload["focus"] == "scoring"
    assert payload["similarity"]["state"] == "fresh"
    assert payload["comparison"]["player_a"]["player_name"] == "Tyrese Maxey"
    assert payload["comparison"]["player_a"]["metric_rows"][1]["label"] == "PTS"
    assert payload["comparison"]["player_b"]["state"] == "unavailable"


def test_api_compare_supports_playoff_window() -> None:
    client = build_client()
    response = client.get("/api/compare?player_a_id=7&player_b_id=9&window=playoffs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["window"] == "playoffs"
    assert payload["window_label"] == "Playoffs"
    assert payload["comparison"]["player_a"]["games_in_window"] == 12
    assert payload["comparison"]["player_a"]["window_games_expected"] is None


def test_api_compare_rejects_duplicate_selection() -> None:
    client = build_client()
    response = client.get("/api/compare?player_a_id=7&player_b_id=7")

    assert response.status_code == 400
    assert response.json()["detail"] == "Compare players must be different"


def test_compare_page_logs_degraded_compare_side(caplog) -> None:
    client = build_client()

    with caplog.at_level("INFO", logger=LOGGER_NAME):
        response = client.get("/compare?player_a_id=7&player_b_id=9&window=last_7")

    assert response.status_code == 200
    events = [
        json.loads(record.message)
        for record in caplog.records
        if "panel_degraded" in record.message
    ]
    assert any(
        event["panel"] == "compare_side"
        and event["reason"] == "player_not_ranked"
        and event["slot"] == "player_b"
        for event in events
    )


def test_player_page_logs_degraded_panel_state(caplog) -> None:
    client = build_client(MissingOpportunityRepository())

    with caplog.at_level("INFO", logger=LOGGER_NAME):
        response = client.get("/players/7/content")

    assert response.status_code == 200
    events = [
        json.loads(record.message)
        for record in caplog.records
        if "panel_degraded" in record.message
    ]
    assert any(
        event["panel"] == "opportunity"
        and event["reason"] == "missing_schedule_context"
        and event["player_id"] == 7
        for event in events
    )


def test_player_page_logs_degraded_heavy_panel_states(caplog) -> None:
    client = build_client()

    with caplog.at_level("INFO", logger=LOGGER_NAME):
        response = client.get("/players/9/content")

    assert response.status_code == 200
    events = [
        json.loads(record.message)
        for record in caplog.records
        if "panel_degraded" in record.message
    ]
    panels = {event["panel"] for event in events}
    assert {"game_log", "trends", "similarity"}.issubset(panels)


def test_compare_page_does_not_log_stale_freshness_banner(caplog) -> None:
    client = build_client(StaleRepository())

    with caplog.at_level("INFO", logger=LOGGER_NAME):
        response = client.get("/compare")

    assert response.status_code == 200
    events = [
        json.loads(record.message)
        for record in caplog.records
        if "panel_degraded" in record.message
    ]
    assert not any(event["panel"] == "freshness_banner" for event in events)


def test_api_player_game_log_success() -> None:
    client = build_client()
    response = client.get("/api/players/7/game-log")

    assert response.status_code == 200
    payload = response.json()
    assert payload["item"]["player_id"] == 7
    assert payload["item"]["player_name"] == "Tyrese Maxey"
    assert len(payload["item"]["games"]) > 0
    assert "pts" in payload["item"]["games"][0]


def test_api_player_game_log_with_limit() -> None:
    client = build_client()
    response = client.get("/api/players/7/game-log?limit=3")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["item"]["games"]) == 3


def test_api_player_game_log_with_date_range() -> None:
    client = build_client()
    response = client.get(
        "/api/players/7/game-log?start_date=2026-02-02&end_date=2026-02-03"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["item"]["date_range"] == {
        "start_date": "2026-02-02",
        "end_date": "2026-02-03",
    }
    assert [game["game_date"] for game in payload["item"]["games"]] == [
        "2026-02-02",
        "2026-02-03",
    ]


def test_api_player_game_log_rejects_reversed_date_range() -> None:
    client = build_client()
    response = client.get(
        "/api/players/7/game-log?start_date=2026-02-03&end_date=2026-02-02"
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "start_date must be on or before end_date"


def test_api_player_game_log_404() -> None:
    client = build_client()
    response = client.get("/api/players/999/game-log")

    assert response.status_code == 404


def test_performance_page_smoke() -> None:
    client = build_client()
    response = client.get("/performance")

    assert response.status_code == 200
    assert "Performance" in response.text
    assert "/static/performance.js" in response.text
    assert f"performance.js?v={STATIC_VERSION}" in response.text
    assert "data-health-status" in response.text
    assert 'data-health-format="season-coverage"' not in response.text
    assert "performance-date-select" in response.text
    assert "Player View" in response.text
    assert "data-performance-modal" in response.text
    assert "performance-modal-detail-link" in response.text
    assert "View Details" in response.text
    assert "Percentile ranges load after selecting a player." not in response.text
    assert "FG%" in response.text
    assert "3PM" in response.text
    assert "data-performance-rating-info" in response.text
    assert 'aria-label="How P-Rating works"' in response.text
    assert "PTS, REB, AST, STL, and BLK are converted to z-scores" in response.text
    assert "MIN, 3PM, FG%, and FT% are context only" in response.text


def test_performance_page_does_not_block_on_health() -> None:
    client = build_client(repo=HealthExplodingRepository())
    response = client.get("/performance")

    assert response.status_code == 200
    assert "Last refresh loading" not in response.text


def test_api_performance_initial_returns_boot_payload_and_uses_cache() -> None:
    repo = CountingPerformanceRepository()
    client = build_client(repo=repo)

    first = client.get("/api/performance/initial")
    second = client.get("/api/performance/initial")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.headers["cache-control"] == "public, max-age=60"
    payload = first.json()
    assert payload["season"] == "2025-26"
    assert payload["selected_date"] == "2026-02-10"
    assert payload["dates"][0]["value"] == "2026-02-10"
    assert payload["games"][0]["game_id"] == "002250010"
    assert payload["players"][0]["player_name"] == "Tyrese Maxey"
    assert repo.initial_calls == 1
    assert repo.date_calls == 0
    assert repo.game_calls == 0
    assert repo.player_calls == 0


def test_prewarm_performance_flow_populates_default_payload_cache() -> None:
    repo = CountingPerformanceRepository()
    key = _performance_initial_cache_key(
        repo,
        game_date=None,
        game_id=None,
        limit=240,
    )
    with _payload_cache_lock:
        _payload_cache.pop(key, None)
        _payload_refreshing.discard(key)
        building = _payload_building.pop(key, None)
        _health_cache.pop(id(repo), None)
    if building is not None:
        building.set()

    try:
        _prewarm_performance_flow(repo)

        with _payload_cache_lock:
            cached = _payload_cache.get(key)

        assert cached is not None
        assert cached[1]["selected_date"] == "2026-02-10"
        assert cached[1]["players"][0]["player_name"] == "Tyrese Maxey"
        assert repo.health_calls == 1
        assert repo.initial_calls == 1
        assert repo.date_calls == 0
        assert repo.game_calls == 0
        assert repo.player_calls == 0
    finally:
        with _payload_cache_lock:
            _payload_cache.pop(key, None)
            _payload_refreshing.discard(key)
            building = _payload_building.pop(key, None)
            _health_cache.pop(id(repo), None)
        if building is not None:
            building.set()


def test_prewarm_performance_flow_tolerates_source_failures() -> None:
    _prewarm_performance_flow(ExplodingPrewarmRepository())


def test_cached_payload_serves_stale_while_refreshing() -> None:
    key = ("unit", "stale-performance-cache")
    calls = 0
    with _payload_cache_lock:
        _payload_cache[key] = (time.monotonic() - 5, {"state": "stale"})
        _payload_refreshing.discard(key)

    def builder() -> dict:
        nonlocal calls
        calls += 1
        return {"state": "fresh"}

    try:
        payload = _get_cached_payload(
            key,
            1,
            builder,
            stale_ttl_seconds=60,
        )

        assert payload == {"state": "stale"}
        for _ in range(50):
            with _payload_cache_lock:
                cached = _payload_cache.get(key)
                is_refreshing = key in _payload_refreshing
            if cached is not None and cached[1] == {"state": "fresh"}:
                break
            if not is_refreshing:
                break
            time.sleep(0.01)

        with _payload_cache_lock:
            cached = _payload_cache.get(key)
        assert cached is not None
        assert cached[1] == {"state": "fresh"}
        assert calls == 1
    finally:
        with _payload_cache_lock:
            _payload_cache.pop(key, None)
            _payload_building.pop(key, None)
            _payload_refreshing.discard(key)


def test_cached_payload_coalesces_concurrent_cold_builds() -> None:
    key = ("unit", "cold-performance-cache")
    calls = 0
    release = Event()
    results: list[dict[str, str]] = []

    with _payload_cache_lock:
        _payload_cache.pop(key, None)
        _payload_building.pop(key, None)
        _payload_refreshing.discard(key)

    def builder() -> dict[str, str]:
        nonlocal calls
        calls += 1
        release.wait(timeout=2)
        return {"state": "fresh"}

    def read_payload() -> None:
        results.append(_get_cached_payload(key, 60, builder))

    threads = [Thread(target=read_payload), Thread(target=read_payload)]
    try:
        for thread in threads:
            thread.start()
        for _ in range(50):
            with _payload_cache_lock:
                is_building = key in _payload_building
            if is_building:
                break
            time.sleep(0.01)
        release.set()
        for thread in threads:
            thread.join(timeout=2)

        assert results == [{"state": "fresh"}, {"state": "fresh"}]
        assert calls == 1
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=2)
        with _payload_cache_lock:
            _payload_cache.pop(key, None)
            building = _payload_building.pop(key, None)
            _payload_refreshing.discard(key)
        if building is not None:
            building.set()


def test_api_performance_dates() -> None:
    client = build_client()
    response = client.get("/api/performance/dates")

    assert response.status_code == 200
    payload = response.json()
    assert payload["season"] == DEFAULT_SEASON
    assert payload["items"][0]["value"] == "2026-02-10"


def test_api_performance_games_filters_by_date() -> None:
    client = build_client()
    response = client.get("/api/performance/games?game_date=2026-02-10")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["game_id"] == "002250010"
    assert item["matchup"] == "NYK @ PHI"
    assert item["players_played"] == 20


def test_api_performance_players_returns_baseline_deltas() -> None:
    client = build_client()
    response = client.get(
        "/api/performance/players?game_date=2026-02-10&game_id=002250010"
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["player_name"] == "Tyrese Maxey"
    assert item["performance_status"] == "above"
    assert item["metrics"][0]["label"] == "PTS"
    assert item["metrics"][0]["delta"] == 5.2
    metrics = {metric["key"]: metric for metric in item["metrics"]}
    assert metrics["fg_pct"]["value"] == 50.0
    assert metrics["ft_pct"]["format"] == "percent"
    assert metrics["fg3m"]["value"] == 3
    assert "trend_30d" not in item


def test_api_performance_player_detail_returns_percentile_ranges() -> None:
    client = build_client()
    response = client.get("/api/performance/players/7?game_id=002250010")

    assert response.status_code == 200
    item = response.json()["item"]
    assert item["player_name"] == "Tyrese Maxey"
    assert item["metrics"][0]["percentile"] == 82.0
    assert item["metrics"][0]["range"]["p25"] == 22
    metrics = {metric["key"]: metric for metric in item["metrics"]}
    assert metrics["fg_pct"]["range"]["p25"] == 43.0
    assert item["trend_30d"]["window_days"] == 30
    assert item["trend_30d"]["stats"][0]["season_average"] == 25.8
    assert item["trend_30d"]["points"][-1]["game_id"] == "002250010"
    assert item["trend_30d"]["points"][-1]["fg_pct"] == 50.0


def test_visualize_page_smoke() -> None:
    client = build_client()
    response = client.get("/visualize", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/performance"


def test_visualize_page_with_player_id() -> None:
    client = build_client()
    response = client.get("/visualize?player_id=7", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/performance"


def test_api_similarity_map_returns_players_and_archetypes() -> None:
    client = build_client()
    response = client.get("/api/similarity-map")

    assert response.status_code == 200
    payload = response.json()
    assert payload["season"] == "2025-26"
    assert payload["players"][0]["player_name"] == "Alpha Guard"
    assert payload["players"][0]["x"] == 0.12
    assert payload["archetypes"] == [{"archetype_label": "Scoring Guard", "count": 1}]
    assert payload["models"][0]["model_key"] == "kmeans"
    assert (
        payload["players"][0]["model_assignments"][0]["model_label"]
        == "KMeans baseline"
    )
    assert payload["axes"][0]["key"] == "proj_x"
    assert payload["axes"][0]["drivers"] == ["scoring volume", "usage"]


def test_similarity_map_page_smoke() -> None:
    client = build_client()
    response = client.get("/similarity-map")

    assert response.status_code == 200
    assert "Statistical Profile Similarity" in response.text
    assert f"/static/similarity_map.js?v={STATIC_VERSION}" in response.text
    assert "data-health-status" in response.text
    assert "plotly-gl3d" in response.text
    assert 'id="map-axes-note"' in response.text
    assert 'id="map-model-controls"' in response.text


def test_similarity_map_page_does_not_block_on_health() -> None:
    client = build_client(repo=HealthExplodingRepository())
    response = client.get("/similarity-map")

    assert response.status_code == 200
    assert "Data status loading" not in response.text


def test_api_similarity_map_neighbors_returns_ranked_matches() -> None:
    client = build_client()
    response = client.get("/api/similarity-map/neighbors/1?limit=5")

    assert response.status_code == 200
    payload = response.json()
    assert payload["player_id"] == 1
    assert payload["neighbors"][0]["player_name"] == "Beta Guard"
    assert payload["neighbors"][0]["similarity_score"] == 0.91


def test_api_similarity_map_neighbors_rejects_out_of_range_limit() -> None:
    client = build_client()
    response = client.get("/api/similarity-map/neighbors/1?limit=99")

    assert response.status_code == 422


def test_similarity_map_page_has_search_and_panel() -> None:
    client = build_client()
    response = client.get("/similarity-map")

    assert response.status_code == 200
    assert 'id="map-search-input"' in response.text
    assert 'id="map-panel"' in response.text
    assert "nearest matches in the full feature space" in response.text


class WhatChangedRepository(FakeRepository):
    def __init__(self):
        self.calls = []

    def get_what_changed(self, **kwargs):
        self.calls.append(kwargs)
        return {"state": "empty", "items": [], **kwargs}


def test_what_changed_page_and_parameterized_cached_endpoint():
    repo = WhatChangedRepository()
    client = build_client(repo)
    page = client.get("/what-changed")
    assert page.status_code == 200
    assert "Trending" in page.text
    assert f"what_changed.js?v={STATIC_VERSION}" in page.text
    assert "Top performers" in page.text
    assert "DNP-CD" in page.text
    response = client.get(
        "/api/what-changed?period=week&season_type=Playoffs&as_of=2026-06-14"
    )
    assert response.status_code == 200
    assert response.json()["period"] == "week"
    assert repo.calls == [
        {"period": "week", "season_type": "Playoffs", "as_of": "2026-06-14"}
    ]
    assert (
        client.get(
            "/api/what-changed?period=week&season_type=Playoffs&as_of=2026-06-14"
        ).status_code
        == 200
    )
    assert len(repo.calls) == 1
    assert client.get("/api/what-changed?period=unsafe").status_code == 422
    assert client.get("/api/what-changed?as_of=not-a-date").status_code == 422
    assert client.get("/api/what-changed?season_type=Preseason").status_code == 422


def test_what_changed_source_failure_is_not_a_partial_league_ranking():
    from app.what_changed import WhatChangedUnavailable

    class MissingRepository(WhatChangedRepository):
        def get_what_changed(self, **kwargs):
            raise WhatChangedUnavailable("private warehouse diagnostic")

    response = build_client(MissingRepository()).get("/api/what-changed")
    assert response.status_code == 503
    assert "private warehouse diagnostic" not in response.text


def test_remote_client_cannot_read_clear_or_append_server_history(tmp_path):
    history_path = tmp_path / "history.jsonl"
    history_path.write_text('{"synthetic":"private"}\n')
    build_client(
        settings=_test_settings(
            openai_api_key="test-key",
            agent_history_enabled=True,
            agent_history_path=str(history_path),
        ),
        agent_client=FakeOpenAIClient(),
    )
    remote = TestClient(app, base_url="http://localhost", client=("192.0.2.9", 4000))
    for method, path, body in [
        ("GET", "/api/agent/history", None),
        ("DELETE", "/api/agent/history", None),
        ("POST", "/api/agent/ask", {"question": "Tyrese Maxey points?"}),
        ("POST", "/api/agent/ask/stream", {"question": "Tyrese Maxey points?"}),
    ]:
        assert (
            remote.request(
                method, path, json=body, headers={"X-Forwarded-For": "127.0.0.1"}
            ).status_code
            == 403
        )
    assert history_path.read_text() == '{"synthetic":"private"}\n'


def test_untrusted_client_cannot_bypass_limit_by_rotating_forwarded_header(monkeypatch):
    # Keep both calls in one explicit minute; wall-clock boundary crossings
    # must not turn a valid limiter into a flaky regression result.
    from app.rate_limit import InMemoryRateLimitStore

    monkeypatch.setattr("app.rate_limit.time", lambda: 1_800_000_000.0)
    monkeypatch.setattr("app.rate_limit._memory_store", InMemoryRateLimitStore())
    build_client(
        settings=_test_settings(
            openai_api_key="test-key", agent_rate_limit_per_minute=1
        ),
        agent_client=FakeOpenAIClient(),
    )
    remote = TestClient(app, client=("192.0.2.77", 50000))
    assert (
        remote.post(
            "/api/agent/ask",
            json={"question": "Tyrese Maxey points?"},
            headers={"X-Forwarded-For": "198.51.100.91"},
        ).status_code
        == 200
    )
    assert (
        remote.post(
            "/api/agent/ask",
            json={"question": "Tyrese Maxey points?"},
            headers={"X-Forwarded-For": "198.51.100.92"},
        ).status_code
        == 429
    )


def test_retired_legacy_analytics_endpoints_are_absent():
    client = build_client()
    for path in (
        "/api/leaderboard",
        "/api/trends",
        "/api/analysis/latest",
        "/api/recommendations",
    ):
        assert client.get(path).status_code == 404


@pytest.mark.parametrize("endpoint", ["/api/agent/ask", "/api/agent/ask/stream"])
@pytest.mark.parametrize("fail", [None, "execution", "unexpected"])
def test_ask_logs_do_not_capture_private_request_or_provider_content(
    endpoint, fail, monkeypatch, caplog
):
    from app.agent.service import AgentExecutionError

    marker = "private-content-do-not-log"
    client = build_client(
        settings=_test_settings(
            openai_api_key="test-key",
            agent_rate_limit_per_minute=0,
            agent_rate_limit_daily=0,
        )
    )

    def answer(question, **kwargs):
        trace = kwargs["trace"]
        trace.set_plan(route="player_trend", confidence=1.0)
        record = trace.add_tool(
            name="resolve_player",
            args={"name": marker},
            status="ok",
            duration_ms=1,
            result={"message": marker},
        )
        if fail:
            error = AgentExecutionError if fail == "execution" else RuntimeError
            raise error(marker)
        trace.outcome = "answered"
        return {"answer": marker, "tool_calls": [record]}

    monkeypatch.setattr(
        "app.main._build_stats_agent", lambda *args: SimpleNamespace(answer=answer)
    )
    with caplog.at_level("INFO", logger="app.agent"):
        response = client.post(
            endpoint,
            json={"question": marker, "conversation_id": marker},
            headers={"X-Request-ID": marker},
        )
    assert response.status_code == (502 if fail and endpoint.endswith("ask") else 200)
    assert marker not in caplog.text
    assert not any(record.exc_info for record in caplog.records)
    summaries = [
        json.loads(r.message)
        for r in caplog.records
        if "agent_request_summary" in r.message
    ]
    assert summaries[-1]["request_id"] == response.headers["x-request-id"]
    assert summaries[-1]["outcome"] == ("error" if fail else "answered")
    if not fail:
        assert marker in response.text  # The user's own response still has its detail.


def test_invalid_model_is_not_copied_to_service_logs(caplog):
    marker = "private-invalid-model"
    client = build_client(settings=_test_settings(openai_api_key="test-key"))
    with caplog.at_level("INFO", logger="app.agent"):
        response = client.post(
            "/api/agent/ask", json={"question": "stats", "model": marker}
        )
    assert response.status_code == 400
    assert marker not in caplog.text


def test_players_landing_retains_research_alias():
    client = build_client()
    for path in ("/players", "/research"):
        response = client.get(path)
        assert response.status_code == 200
        assert "<h1>Players</h1>" in response.text
        assert 'id="players-search"' in response.text
        assert "data-research-root" not in response.text
        assert "Three focused questions" not in response.text
        assert "Explore the breakdown" not in response.text


def test_player_shell_does_not_query_warehouse() -> None:
    repo = CountingPlayerDetailRepository()
    client = build_client(repo)
    response = client.get("/players/7?season=2024-25")
    assert response.status_code == 200
    assert 'data-player-id="7"' in response.text
    assert 'aria-busy="true"' in response.text
    assert "Loading profile" in response.text
    assert DEFAULT_SEASON in response.text
    assert not response.url.query
    assert repo.detail_calls == []


def test_missing_player_fragment_returns_not_found() -> None:
    client = build_client()
    assert client.get("/players/999/content").status_code == 404


def test_main_pages_omit_season_selector() -> None:
    client = build_client()
    for route in (
        "/ask",
        "/players",
        "/players/7",
        "/performance",
        "/what-changed",
        "/similarity-map",
    ):
        response = client.get(f"{route}?season=2024-25")
        assert response.status_code == 200
        assert "data-season-selector" not in response.text
