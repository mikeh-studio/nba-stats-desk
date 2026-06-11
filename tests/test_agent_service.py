from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.conversation import InMemoryConversationStore
from app.agent.observability import AgentTrace
from app.agent.service import (
    AgentDisabledError,
    StatsAgent,
    _AnswerFieldStream,
    _clarify_reply_name,
    normalize_agent_answer,
)
from app.config import Settings


def _settings(**overrides: object) -> Settings:
    values = {
        "project_id": "local-project",
        "gold_dataset": "nba_gold",
        "metadata_dataset": "nba_metadata",
        "freshness_threshold_hours": 36,
        "max_search_results": 12,
        "openai_api_key": "test-key",
        "openai_agent_model": "gpt-5.4-mini",
        "openai_agent_enabled": True,
        "agent_max_tool_calls": 6,
        "agent_rate_limit_per_minute": 0,
    }
    values.update(overrides)
    return Settings(**values)


class AgentServiceFakeRepository:
    def search_players(self, query: str, limit: int = 10) -> list[dict]:
        return [
            {
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "latest_team_abbr": "PHI",
                "latest_game_date": "2026-02-10",
                "games_sampled": 12,
                "sample_status": "ready",
                "overall_rank": 12,
            }
        ][:limit]

    def get_player_detail(self, player_id: int) -> dict | None:
        if player_id != 7:
            return None
        return {
            "player": {
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "team_abbr": "PHI",
                "games_sampled": 12,
            },
            "trends": [{"stat": "PTS", "label": "PTS", "delta": 4.2}],
            "game_log": {
                "games": [
                    {
                        "game_date": "2026-02-10",
                        "matchup": "PHI vs. NYK",
                        "wl": "W",
                        "pts": "30",
                        "reb": "4",
                        "ast": "8",
                        "stl": "1",
                        "blk": "0",
                        "tov": "2",
                    }
                ]
            },
            "chart_baselines": {},
            "similar_players": [{"player_id": 11, "player_name": "Jalen Brunson"}],
            "stat_percentiles": [
                {"key": "pts", "label": "PTS", "average": 25.0, "percentile": 91.0}
            ],
        }

    def get_player_game_log(
        self,
        player_id: int,
        limit: int = 30,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict | None:
        if player_id != 7:
            return None
        return {
            "player_id": 7,
            "player_name": "Tyrese Maxey",
            "season": "2025-26",
            "games": [
                {
                    "game_date": "2026-02-10",
                    "matchup": "PHI vs. NYK",
                    "wl": "W",
                    "pts": "30",
                    "reb": "4",
                    "ast": "8",
                    "stl": "1",
                    "blk": "0",
                    "tov": "2",
                }
            ][:limit],
        }

    def get_metric_leaders(self, metric: str, limit: int = 10) -> list[dict]:
        return [
            {
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "metric_key": metric,
                "metric_value": 30.0,
            }
        ][:limit]

    def get_player_metric_percentile(
        self, player_id: int, metric: str, min_games: int = 5
    ) -> dict | None:
        if player_id != 7:
            return None
        return {
            "player_name": "Tyrese Maxey",
            "metric_key": metric,
            "percentile": 91.0,
            "in_requested_cohort": True,
        }


class SequenceResponses:
    def __init__(self, tool_calls: list[SimpleNamespace]) -> None:
        self.calls = 0
        self.kwargs: list[dict] = []
        self.tool_calls = tool_calls

    def create(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        if self.calls == 1 and self.tool_calls:
            return SimpleNamespace(output=self.tool_calls, output_text="")
        return SimpleNamespace(
            output=[],
            output_text=json.dumps(
                {
                    "answer": "Answered from mocked tools.",
                    "assumptions": [],
                    "tables": [],
                    "charts": [],
                    "metric_definitions": [],
                    "followups": [],
                }
            ),
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
        )


class SequenceClient:
    def __init__(self, tool_calls: list[SimpleNamespace]) -> None:
        self.responses = SequenceResponses(tool_calls)


class FlakyResponses(SequenceResponses):
    def create(self, **kwargs):
        if self.calls == 0:
            self.calls += 1
            exc = TimeoutError("temporary timeout")
            raise exc
        return super().create(**kwargs)


class FlakyClient:
    def __init__(self) -> None:
        self.responses = FlakyResponses([])


def _call(name: str, arguments: dict, call_id: str = "call_1") -> SimpleNamespace:
    return SimpleNamespace(
        type="function_call",
        name=name,
        arguments=json.dumps(arguments),
        call_id=call_id,
    )


def test_normalize_agent_answer_handles_malformed_payloads() -> None:
    assert normalize_agent_answer(None)["answer"] == "No answer returned."
    assert normalize_agent_answer("plain text")["answer"] == "plain text"

    payload = normalize_agent_answer({"answer": "ok", "tables": "bad"})

    assert payload["answer"] == "ok"
    assert payload["tables"] == []
    assert payload["charts"] == []


def test_clarification_short_circuits_without_openai() -> None:
    client = SequenceClient([])
    trace = AgentTrace("req-1", "stats", "gpt-5.4-mini")
    agent = StatsAgent(_settings(), AgentServiceFakeRepository(), client=client)

    payload = agent.answer("stats", conversation_id="c1", trace=trace)

    assert payload["agent_plan"]["route"] == "clarify"
    assert payload["tool_calls"] == []
    assert payload["answer"].startswith("Which player")
    assert client.responses.calls == 0
    assert trace.outcome == "clarified"


def test_clarification_still_requires_configured_agent_without_client() -> None:
    agent = StatsAgent(
        _settings(openai_api_key=None),
        AgentServiceFakeRepository(),
    )

    try:
        agent.answer("stats")
    except AgentDisabledError as exc:
        assert "OPENAI_API_KEY" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("Expected missing-key clarification to be disabled")


def test_disabled_agent_rejects_even_with_injected_client() -> None:
    agent = StatsAgent(
        _settings(openai_agent_enabled=False),
        AgentServiceFakeRepository(),
        client=SequenceClient([]),
    )

    try:
        agent.answer("How is Tyrese Maxey trending?")
    except AgentDisabledError as exc:
        assert "disabled" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("Expected disabled agent to reject injected client")


def test_route_hint_and_required_tool_order_reach_openai() -> None:
    client = SequenceClient(
        [_call("resolve_player", {"name": "Tyrese Maxey", "limit": 5})]
    )
    agent = StatsAgent(_settings(), AgentServiceFakeRepository(), client=client)

    payload = agent.answer("How is Tyrese Maxey trending?", conversation_id="c1")

    assert payload["tool_calls"][0]["name"] == "resolve_player"
    answer_kwargs = client.responses.kwargs[-1]
    developer_messages = [
        message["content"]
        for message in answer_kwargs["input"]
        if message["role"] == "developer"
    ]
    assert any("route=player_trend" in message for message in developer_messages)
    assert answer_kwargs.get("tools") is None
    assert [tool["name"] for tool in payload["tool_calls"][:2]] == [
        "resolve_player",
        "get_player_trends",
    ]


def test_resolved_player_answer_includes_compact_player_profile() -> None:
    client = SequenceClient([])
    agent = StatsAgent(_settings(), AgentServiceFakeRepository(), client=client)

    payload = agent.answer("How is Tyrese Maxey trending?")

    profile = payload["player_profile"]
    assert profile["profile_url"] == "/players/7"
    assert profile["player"]["player_id"] == 7
    assert profile["player"]["player_name"] == "Tyrese Maxey"
    assert profile["player"]["team_abbr"] == "PHI"
    assert profile["player"]["games_sampled"] == 12
    assert profile["similar_players"][0]["player_name"] == "Jalen Brunson"


def test_transient_openai_errors_retry() -> None:
    client = FlakyClient()
    agent = StatsAgent(
        _settings(openai_agent_retry_base_delay_seconds=0),
        AgentServiceFakeRepository(),
        client=client,
    )

    payload = agent.answer("Who are the top 5 players by points?")

    assert payload["answer"] == "Answered from mocked tools."
    assert client.responses.calls == 2


def test_percentile_evidence_respects_min_games_filter() -> None:
    client = SequenceClient([])
    agent = StatsAgent(_settings(), AgentServiceFakeRepository(), client=client)

    payload = agent.answer(
        "Which percentile is Tyrese Maxey points among players with at least 50 games?"
    )

    percentile_call = next(
        tool
        for tool in payload["tool_calls"]
        if tool["name"] == "calculate_player_percentile"
    )
    assert payload["agent_plan"]["min_games"] == 50
    assert percentile_call["args"]["min_games"] == 50


def test_tool_limit_outputs_are_matched_for_remaining_batch_calls() -> None:
    client = SequenceClient(
        [
            _call("resolve_player", {"name": "Tyrese Maxey", "limit": 5}, "call_1"),
            _call(
                "get_player_trends",
                {
                    "player_id": 7,
                    "metrics": ["pts"],
                    "start_date": None,
                    "end_date": None,
                },
                "call_2",
            ),
        ]
    )
    agent = StatsAgent(
        _settings(agent_max_tool_calls=1),
        AgentServiceFakeRepository(),
        client=client,
    )

    payload = agent.answer("How is Tyrese Maxey trending?")

    assert payload["answer"].startswith("I hit the tool-call limit")
    assert [tool["status"] for tool in payload["tool_calls"]] == ["ok", "tool_limit"]


def test_conversation_memory_replays_prior_turns() -> None:
    store = InMemoryConversationStore()
    client = SequenceClient([])
    agent = StatsAgent(
        _settings(),
        AgentServiceFakeRepository(),
        client=client,
        conversation_store=store,
    )

    agent.answer("How is Tyrese Maxey trending?", conversation_id="thread-1")
    agent.answer("What about assists?", conversation_id="thread-1")

    second_input = client.responses.kwargs[-1]["input"]
    assert second_input[0]["role"] == "user"
    assert second_input[0]["content"] == "How is Tyrese Maxey trending?"
    assert second_input[1]["role"] == "assistant"
    user_contents = [
        message["content"] for message in second_input if message["role"] == "user"
    ]
    assert user_contents[-1] == "What about assists?"
    assert any(message["role"] == "developer" for message in second_input)


def test_conversation_memory_zero_turns_disables_replay() -> None:
    store = InMemoryConversationStore()

    store.append_turn(
        "thread-1",
        question="How is Tyrese Maxey trending?",
        answer="Answered.",
        max_turns=0,
    )

    assert store.get_turns("thread-1", max_turns=0) == []
    assert store.get_turns("thread-1", max_turns=6) == []


class AmbiguousPlayerRepository(AgentServiceFakeRepository):
    """Fake repo where 'Jalen' is ambiguous and 'zzz' queries find nobody."""

    def search_players(self, query: str, limit: int = 10) -> list[dict]:
        lowered = query.lower()
        if "zzz" in lowered:
            return []
        if "jalen" in lowered:
            return [
                {
                    "player_id": 21,
                    "player_name": "Jalen Green",
                    "latest_team_abbr": "HOU",
                    "latest_game_date": "2026-02-10",
                    "games_sampled": 14,
                    "sample_status": "ready",
                    "overall_rank": 30,
                },
                {
                    "player_id": 22,
                    "player_name": "Jalen Williams",
                    "latest_team_abbr": "OKC",
                    "latest_game_date": "2026-02-10",
                    "games_sampled": 15,
                    "sample_status": "ready",
                    "overall_rank": 18,
                },
            ][:limit]
        return super().search_players(query, limit)


def _clarify_agent(
    store: InMemoryConversationStore,
) -> tuple[StatsAgent, SequenceClient]:
    client = SequenceClient([])
    agent = StatsAgent(
        _settings(),
        AmbiguousPlayerRepository(),
        client=client,
        conversation_store=store,
    )
    return agent, client


def test_clarify_reply_name_extraction() -> None:
    assert _clarify_reply_name("Jalen Williams") == "Jalen Williams"
    assert _clarify_reply_name("I meant Jalen Williams") == "Jalen Williams"
    assert _clarify_reply_name("no, Jalen Green.") == "Jalen Green"
    assert (
        _clarify_reply_name("Who are the top 10 players by points this season?") is None
    )


def test_ambiguous_player_stores_pending_clarification_with_options() -> None:
    store = InMemoryConversationStore()
    agent, client = _clarify_agent(store)

    payload = agent.answer("How is Jalen trending?", conversation_id="thread-1")

    names = [option["player_name"] for option in payload["clarification_options"]]
    assert names == ["Jalen Williams", "Jalen Green"]
    assert payload["answer"] == "Which player did you mean?"
    pending = store.get_pending_clarification("thread-1")
    assert pending is not None
    assert pending.question == "How is Jalen trending?"
    turns = store.get_turns("thread-1", max_turns=6)
    assert turns[-1].question == "How is Jalen trending?"


def test_clarify_reply_resumes_original_question() -> None:
    store = InMemoryConversationStore()
    agent, client = _clarify_agent(store)

    agent.answer("How is Jalen trending?", conversation_id="thread-1")
    payload = agent.answer("I meant Jalen Williams", conversation_id="thread-1")

    assert payload["answer"] == "Answered from mocked tools."
    assert store.get_pending_clarification("thread-1") is None
    resolve_call = payload["tool_calls"][0]
    assert resolve_call["name"] == "resolve_player"
    assert resolve_call["args"]["name"] == "Jalen Williams"
    # The agent answers the original question, not the clarification reply.
    final_input = client.responses.kwargs[-1]["input"]
    user_contents = [
        message["content"] for message in final_input if message["role"] == "user"
    ]
    assert user_contents[-1] == "How is Jalen trending?"


def test_clarify_option_click_resumes_with_selected_player() -> None:
    store = InMemoryConversationStore()
    agent, _ = _clarify_agent(store)

    agent.answer("How is Jalen trending?", conversation_id="thread-1")
    payload = agent.answer(
        "How is Jalen trending?",
        conversation_id="thread-1",
        selected_player={"player_id": 22, "player_name": "Jalen Williams"},
    )

    assert payload["answer"] == "Answered from mocked tools."
    assert store.get_pending_clarification("thread-1") is None
    resolve_call = payload["tool_calls"][0]
    assert resolve_call["name"] == "resolve_player"
    assert resolve_call["args"]["name"] == "Jalen Williams"
    pinned = payload["query_plan"]["resolved_players"][0]
    assert pinned["player_id"] == 22
    assert pinned["match_method"] == "user_selected"


def test_failed_clarify_reply_reasks_and_keeps_pending() -> None:
    store = InMemoryConversationStore()
    agent, _ = _clarify_agent(store)

    agent.answer("How is Jalen trending?", conversation_id="thread-1")
    payload = agent.answer("Zzz Qqq", conversation_id="thread-1")

    assert 'could not find a player matching "Zzz Qqq"' in payload["answer"]
    pending = store.get_pending_clarification("thread-1")
    assert pending is not None
    assert pending.question == "How is Jalen trending?"


def test_new_question_while_pending_drops_clarification() -> None:
    store = InMemoryConversationStore()
    agent, _ = _clarify_agent(store)

    agent.answer("How is Jalen trending?", conversation_id="thread-1")
    payload = agent.answer(
        "How is Tyrese Maxey trending in the last 10 games?",
        conversation_id="thread-1",
    )

    assert store.get_pending_clarification("thread-1") is None
    assert payload["answer"] == "Answered from mocked tools."
    assert payload["clarification_options"] == []


def test_pending_clarification_store_roundtrip_and_eviction() -> None:
    store = InMemoryConversationStore(max_conversations=2)

    store.set_pending_clarification(
        "thread-a", question="How is Jalen trending?", query_plan=None
    )
    assert store.get_pending_clarification("thread-a") is not None

    store.clear_pending_clarification("thread-a")
    assert store.get_pending_clarification("thread-a") is None

    store.set_pending_clarification(
        "thread-a", question="How is Jalen trending?", query_plan=None
    )
    store.append_turn("thread-b", question="qb", answer="ab", max_turns=4)
    store.append_turn("thread-c", question="qc", answer="ac", max_turns=4)
    assert store.get_pending_clarification("thread-a") is None


def test_conversation_store_evicts_least_recently_used_conversations() -> None:
    store = InMemoryConversationStore(max_conversations=2)

    store.append_turn("thread-a", question="qa", answer="aa", max_turns=4)
    store.append_turn("thread-b", question="qb", answer="ab", max_turns=4)
    store.append_turn("thread-a", question="qa2", answer="aa2", max_turns=4)
    store.append_turn("thread-c", question="qc", answer="ac", max_turns=4)

    assert store.get_turns("thread-b", max_turns=4) == []
    questions = [turn.question for turn in store.get_turns("thread-a", max_turns=4)]
    assert questions == ["qa", "qa2"]
    assert [turn.question for turn in store.get_turns("thread-c", max_turns=4)] == [
        "qc"
    ]


def test_answer_field_stream_decodes_across_chunk_boundaries() -> None:
    emitted: list[str] = []
    stream = _AnswerFieldStream(emitted.append)
    raw = json.dumps({"answer": 'He said "go" — now\nplay.', "assumptions": []})

    for index in range(0, len(raw), 3):
        stream.feed(raw[index : index + 3])

    assert "".join(emitted) == 'He said "go" — now\nplay.'
    assert stream.emitted is True


def test_answer_field_stream_handles_split_escapes_and_unicode() -> None:
    emitted: list[str] = []
    stream = _AnswerFieldStream(emitted.append)
    # Split right inside the \u escape and the \n escape.
    stream.feed('{"answer": "tip\\u00e9')
    stream.feed('e\\')
    stream.feed('nend", "tables": []}')

    assert "".join(emitted) == "tipée\nend"


def test_answer_field_stream_ignores_payloads_without_answer_string() -> None:
    emitted: list[str] = []
    stream = _AnswerFieldStream(emitted.append)
    stream.feed('{"answer": 42, "assumptions": []}')

    assert emitted == []
    assert stream.emitted is False
