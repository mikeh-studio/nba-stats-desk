"""Offline adapter fault injection: no provider keys, network, or warehouse."""

import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from app.agent import execution
from app.agent.claude_client import ClaudeResponsesClient
from app.agent.execution import ExecutionFailure, create_response, request_policy, stage
from app.agent.observability import AgentTrace
from app.agent.openrouter_client import OpenRouterResponsesClient
from app.agent.semantic_planner import plan_question
from app.agent.semantics import SemanticError
from app.config import OPENROUTER_AGENT_MODEL_VALUES
from tests.test_agent_service import _settings


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, value):
        self.now += value


class StatusError(Exception):
    def __init__(self, status):
        self.status_code = status


class SDK:
    """Actual adapters consume this transport spy; every physical attempt recorded."""

    def __init__(self, provider, scenario, clock):
        self.provider, self.scenario, self.clock = provider, scenario, clock
        self.calls, self.options, self.requests = 0, [], []
        self.responses = self
        self.chat = NS(completions=self)
        self.messages = NS(stream=self.stream)

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self

    def outcome(self, kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        self.clock.now += 0.2
        if self.scenario == "rate_limit" and self.calls == 1:
            raise StatusError(429)
        if self.scenario == "transient" and self.calls == 1:
            raise StatusError(503)
        if self.scenario == "timeout":
            raise TimeoutError("private-provider-body")
        if self.scenario == "permanent":
            raise StatusError(401)
        if self.scenario == "deadline":
            self.clock.now += 100
        return (
            "not-json"
            if self.scenario == "malformed"
            else '{"plan":{"status":"unsupported","queries":[],"message":"Scope unavailable"}}'
        )

    def create(self, **kwargs):
        text = self.outcome(kwargs)
        if self.provider == "openrouter":
            message = NS(
                content=text,
                tool_calls=[],
                refusal=None,
                model_dump=lambda **kw: {"role": "assistant", "content": text},
            )
            return NS(choices=[NS(finish_reason="stop", message=message)], usage=None)
        return NS(output_text=text, output=[], usage=None)

    def stream(self, **kwargs):
        text = self.outcome(kwargs)
        scenario, clock = self.scenario, self.clock

        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            @property
            def text_stream(self):
                yield text[:4]
                if scenario == "partial":
                    raise StatusError(503)
                if scenario == "stream_deadline":
                    clock.now += 100
                yield text[4:]

            def get_final_message(self):
                return NS(content=[NS(type="text", text=text)], usage=None)

        return Stream()


@pytest.fixture
def clock(monkeypatch):
    result = Clock()
    monkeypatch.setattr(execution, "monotonic", result.monotonic)
    monkeypatch.setattr(execution, "sleep", result.sleep)
    return result


def adapter(provider, scenario, clock):
    sdk = SDK(provider, scenario, clock)
    if provider == "claude":
        return (
            ClaudeResponsesClient(client=sdk, model="claude-test"),
            sdk,
            "claude-test",
        )
    if provider == "openrouter":
        return (
            OpenRouterResponsesClient(sdk),
            sdk,
            sorted(OPENROUTER_AGENT_MODEL_VALUES)[0],
        )
    return sdk, sdk, "test-model"


ROWS = []


@pytest.fixture(scope="module", autouse=True)
def optional_report():
    yield
    import hashlib
    import html
    import os
    import subprocess
    from pathlib import Path

    if not os.getenv("ASK_EXECUTION_REPORT"):
        return
    target = Path(os.environ["ASK_EXECUTION_REPORT"])
    target.parent.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    provenance = {
        "base_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "clock": "virtual, seconds",
        "source_sha256": {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in [
                "app/agent/execution.py",
                "app/agent/claude_client.py",
                "app/agent/openrouter_client.py",
                "tests/test_agent_execution.py",
            ]
        },
    }
    target.with_suffix(".json").write_text(
        json.dumps({"provenance": provenance, "results": ROWS}, indent=2)
    )
    columns = [
        "provider",
        "scenario",
        "expected",
        "actual",
        "attempts",
        "timeout",
        "stage_ms",
        "completion_ms",
    ]
    body = "".join(
        "<tr>"
        + "".join("<td>" + html.escape(str(row.get(k, ""))) + "</td>" for k in columns)
        + "</tr>"
        for row in ROWS
    )
    target.write_text(
        '<!doctype html><meta charset="utf-8"><title>Ask execution policy QA</title><style>body{font:16px system-ui;margin:40px;max-width:1300px;background:#101820;color:#eaf0f4}td,th{padding:12px;border-bottom:1px solid #43515d;text-align:left}code{color:#a9d7fc}</style><h1>Ask execution policy QA</h1><p>Offline real-adapter fault injection over synthetic SDK transports. Times are virtual-clock test values, not real provider latency. No network, paid calls, or warehouse access. Every expected/actual outcome is asserted by pytest.</p><p>Baseline code used a fixed semantic planner timeout and inconsistent retry ownership. This report verifies the new contracts; it does not invent historical baseline measurements. Partial output applies to Claude, the only delta-streaming adapter; OpenAI and OpenRouter are buffered.</p><table><thead><tr>'
        + "".join("<th>" + k + "</th>" for k in columns)
        + "</tr></thead><tbody>"
        + body
        + "</tbody></table><h2>Provenance</h2><pre>"
        + html.escape(json.dumps(provenance, indent=2))
        + "</pre>"
    )


@pytest.mark.parametrize("provider", ["openai", "claude", "openrouter"])
@pytest.mark.parametrize(
    "scenario,expected,attempts",
    [
        ("success", "unsupported", 1),
        ("malformed", "invalid_plan", 1),
        ("rate_limit", "unsupported", 2),
        ("transient", "unsupported", 2),
        ("timeout", "provider_timeout", 3),
        ("permanent", "provider_failure", 1),
        ("deadline", "request_timeout", 1),
    ],
)
def test_real_adapter_matrix(provider, scenario, expected, attempts, clock):
    settings = _settings(
        openai_agent_timeout_seconds=7,
        anthropic_agent_timeout_seconds=13,
        openrouter_agent_timeout_seconds=19,
        agent_request_deadline_seconds=30,
        openai_agent_max_retries=2,
    )
    client, sdk, model = adapter(provider, scenario, clock)
    trace = AgentTrace("test-id", "private-question", model)
    with request_policy(settings, provider, trace):
        try:
            plan = plan_question(
                client,
                model=model,
                question="Who leads in points per game?",
                selected_season="2025-26",
            )
            actual = plan["status"]
        except (ExecutionFailure, SemanticError) as exc:
            actual = exc.code
    assert actual == expected
    assert sdk.calls == trace.model_calls == attempts
    assert sdk.options and all(opt["max_retries"] == 0 for opt in sdk.options)
    timeout = {"openai": 7, "claude": 13, "openrouter": 19}[provider]
    assert sdk.options[0]["timeout"] == timeout
    assert trace.stage_latency_ms["planning"] >= 200
    assert "private" not in json.dumps(trace.to_log_dict())
    ROWS.append(
        dict(
            provider=provider,
            scenario=scenario,
            expected=expected,
            actual=actual,
            attempts=sdk.calls,
            timeout=sdk.options[0]["timeout"],
            stage_ms=trace.stage_latency_ms,
            completion_ms=round(clock.now * 1000, 3),
        )
    )


@pytest.mark.parametrize(
    "scenario,expected",
    [("partial", "provider_failure"), ("stream_deadline", "request_timeout")],
)
def test_claude_never_replays_partial_stream(scenario, expected, clock):
    client, sdk, model = adapter("claude", scenario, clock)
    deltas = []
    trace = AgentTrace("test", "", model)
    with request_policy(_settings(agent_request_deadline_seconds=30), "claude", trace):
        with pytest.raises(ExecutionFailure, match=expected):
            create_response(
                client,
                model=model,
                input=[{"role": "user", "content": "test"}],
                text={"format": {"type": "text"}},
                on_output_text_delta=deltas.append,
            )
    assert sdk.calls == trace.model_calls == 1
    assert deltas == ['{"pl']
    ROWS.append(
        dict(
            provider="claude",
            scenario=scenario,
            expected=expected,
            actual=expected,
            attempts=1,
            timeout=sdk.options[0]["timeout"],
            stage_ms=trace.stage_latency_ms,
            completion_ms=round(clock.now * 1000, 3),
        )
    )


def test_retry_backoff_and_next_timeout_consume_remaining_budget(clock):
    settings = _settings(
        agent_request_deadline_seconds=1, openai_agent_retry_base_delay_seconds=0.5
    )
    client, sdk, model = adapter("openai", "transient", clock)
    with request_policy(settings, "openai"):
        create_response(
            client,
            model=model,
            instructions="test",
            input=[{"role": "user", "content": "test"}],
            text={"format": {"type": "text"}},
        )
    assert sdk.options[0]["timeout"] == 1
    assert sdk.options[1]["timeout"] == pytest.approx(0.3)
    assert clock.now == pytest.approx(0.9)


def test_no_retry_when_backoff_exceeds_budget(clock):
    client, sdk, model = adapter("openai", "transient", clock)
    with request_policy(_settings(agent_request_deadline_seconds=0.5), "openai"):
        with pytest.raises(ExecutionFailure, match="request_timeout"):
            create_response(
                client,
                model=model,
                instructions="test",
                input=[{"role": "user", "content": "test"}],
                text={"format": {"type": "text"}},
            )
    assert sdk.calls == 1
    assert clock.now == 0.2


def test_stage_records_failure_and_prevents_downstream_calls(clock):
    trace = AgentTrace("test", "", "test")
    client, sdk, model = adapter("openai", "success", clock)
    with request_policy(_settings(agent_request_deadline_seconds=1), "openai", trace):
        with pytest.raises(ExecutionFailure, match="request_timeout"):
            with stage("evidence"):
                clock.now += 2  # A blocking source cannot be forcibly stopped.
            create_response(
                client,
                model=model,
                instructions="test",
                input=[{"role": "user", "content": "test"}],
                text={"format": {"type": "text"}},
            )
    assert trace.stage_latency_ms == {"evidence": 2000}
    assert sdk.calls == 0


def test_nested_stage_not_double_counted_and_context_is_reset(clock):
    trace = AgentTrace("test", "", "test")
    with request_policy(_settings(), "openai", trace):
        with stage("identity"):
            clock.now += 1
            with stage("identity"):
                clock.now += 2
    assert trace.stage_latency_ms == {"identity": 3000}
    assert execution.CURRENT.get() is None


@pytest.mark.parametrize("provider", ["openai", "claude", "openrouter"])
def test_provider_retry_limits_are_independent(provider, clock):
    client, sdk, model = adapter(provider, "timeout", clock)
    settings = replace(
        _settings(),
        openai_agent_max_retries=0,
        anthropic_agent_max_retries=1,
        openrouter_agent_max_retries=3,
    )
    with request_policy(settings, provider):
        with pytest.raises(ExecutionFailure, match="provider_timeout"):
            create_response(
                client,
                model=model,
                instructions="test",
                input=[{"role": "user", "content": "test"}],
                text={"format": {"type": "text"}},
            )
    assert sdk.calls == {"openai": 1, "claude": 2, "openrouter": 4}[provider]


@pytest.mark.parametrize("endpoint", ["/api/agent/ask", "/api/agent/ask/stream"])
@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("malformed", "invalid_plan"),
        ("timeout", "provider_timeout"),
        ("deadline", "request_timeout"),
        ("permanent", "provider_failure"),
    ],
)
def test_real_endpoints_preserve_terminal_failure_and_privacy(
    endpoint, scenario, expected, clock, tmp_path, caplog
):
    from pathlib import Path

    from app.main import _season_conversation_store, app
    from scripts.ask_eval.fixtures import frozen_repository, load_inputs
    from tests.test_api import build_client

    source, _ = load_inputs(Path(__file__).resolve().parents[1])
    history = tmp_path / "history.jsonl"
    settings = _settings(
        agent_history_enabled=True,
        agent_history_path=str(history),
        agent_rate_limit_daily=0,
        agent_request_deadline_seconds=30,
    )
    model, sdk, _ = adapter("openai", scenario, clock)
    _season_conversation_store.cache_clear()
    repo = frozen_repository(settings, source, "complete")
    client = build_client(repo, settings=settings, agent_client=model)
    with caplog.at_level("INFO", logger="app.agent"):
        response = client.post(
            endpoint,
            json={
                "question": "Avery Example points per game in 2024-25",
                "conversation_id": "execution-failure-test",
            },
        )
    summaries = [
        json.loads(r.message)
        for r in caplog.records
        if "agent_request_summary" in r.message
    ]
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["outcome"] == expected
    assert summary["model_calls"] == sdk.calls
    assert summary["stage_latency_ms"]["planning"] >= 200
    assert "private-provider-body" not in caplog.text
    assert "Avery Example" not in caplog.text
    if scenario != "malformed":
        assert not history.exists()
        assert (
            _season_conversation_store("2025-26").get_turns(
                "execution-failure-test", max_turns=10
            )
            == []
        )
    if endpoint.endswith("/stream"):
        terminal_events = [
            line
            for line in response.text.splitlines()
            if line in {"event: error", "event: final"}
        ]
        assert len(terminal_events) == 1

    else:
        assert response.status_code == (200 if scenario == "malformed" else 502)
    app.dependency_overrides.clear()
    _season_conversation_store.cache_clear()


def test_deterministic_route_has_stage_timings_and_zero_calls(clock):
    from tests.test_ask_harness import agent

    a = agent()
    trace = AgentTrace("test", "", "model")
    answer = a.answer(
        "Who is similar to Avery Example in the last five games in 2024-25?",
        trace=trace,
    )
    assert answer["status"] == "ok"
    assert trace.model_calls == 0
    assert {"routing", "identity", "calculation", "rendering"} <= set(
        trace.stage_latency_ms
    )


@pytest.mark.parametrize("limit", [0, -1, float("inf"), float("nan")])
def test_nonfinite_or_disabled_budgets_are_rejected(limit, clock):
    with pytest.raises(ValueError, match="positive and finite"):
        with request_policy(_settings(agent_request_deadline_seconds=limit), "openai"):
            pass
    client, sdk, model = adapter("openai", "success", clock)
    with request_policy(_settings(openai_agent_timeout_seconds=limit), "openai"):
        with pytest.raises(ValueError, match="positive and finite"):
            create_response(client, model=model)
    assert sdk.calls == 0


def test_late_rendering_deadline_does_not_save_successful_context(clock, monkeypatch):
    from app.agent.visualization import VisualizationAgent
    from tests.test_ask_harness import agent

    a = agent()
    a.settings = replace(a.settings, agent_request_deadline_seconds=1)
    trace = AgentTrace("test", "", "model")

    def late_render(self, agent, payload, *args):
        # The deterministic answer has been built, but final enrichment blocks.
        clock.now += 2
        return payload

    monkeypatch.setattr(VisualizationAgent, "enrich", late_render)
    with pytest.raises(ExecutionFailure, match="request_timeout"):
        a.answer(
            "Who is similar to Avery Example in the last five games in 2024-25?",
            conversation_id="late",
            trace=trace,
        )
    assert a.conversation_store.get_turns("late", max_turns=10) == []
    assert trace.outcome == "request_timeout"


def test_successful_request_commits_context_once(clock):
    from tests.test_ask_harness import agent

    a = agent()
    a.answer(
        "Who is similar to Avery Example in the last five games in 2024-25?",
        conversation_id="success",
    )
    assert len(a.conversation_store.get_turns("success", max_turns=10)) == 1


def test_multiple_model_stages_share_one_request_budget(clock):
    client, sdk, model = adapter("openai", "success", clock)
    with request_policy(_settings(agent_request_deadline_seconds=1), "openai"):
        create_response(client, model=model, timeout_seconds=0.6)
        create_response(client, model=model)
        clock.now = 1
        with pytest.raises(ExecutionFailure, match="request_timeout"):
            create_response(client, model=model)
    assert [option["timeout"] for option in sdk.options] == pytest.approx([0.6, 0.8])
    assert sdk.calls == 2


@pytest.mark.parametrize("elapsed", [0, 115, 119.5])
@pytest.mark.parametrize("failure", ["timeout", "transient"])
def test_optional_chart_failure_preserves_completed_answer(
    elapsed, failure, clock, monkeypatch
):
    from app.agent.visualization import VisualizationAgent, study_charts
    from tests.test_ask_harness import agent
    from tests.test_research_insights import presentation_study

    a = agent()
    a.settings = replace(a.settings, agent_request_deadline_seconds=120)
    sdk = SDK("openai", "success", clock)

    def fail(**kwargs):
        sdk.calls += 1
        if failure == "timeout":
            clock.now += sdk.options[-1]["timeout"]
            raise TimeoutError("private provider body")
        raise StatusError(503)

    monkeypatch.setattr(sdk, "create", fail)
    monkeypatch.setattr(a, "_get_client", lambda provider: sdk)
    trace = AgentTrace("optional", "openai", "test")

    @execution.governed_request
    def completed_answer(self, *, trace):
        clock.now += elapsed
        self.conversation_store.append_turn(
            "optional", question="synthetic", answer="Completed answer", max_turns=10
        )
        return VisualizationAgent().enrich(
            self,
            {
                "answer": "Completed answer",
                "_chart_candidates": study_charts(
                    presentation_study(), {"pts", "ast"}, []
                ),
            },
            "synthetic",
            "openai",
            "test",
            trace,
        )

    result = completed_answer(a, trace=trace)
    assert result["answer"] == "Completed answer"
    assert result["visualization"]["selection"] == "fallback"
    assert result["visualization"]["chart_id"] == "pts"
    assert len(a.conversation_store.get_turns("optional", max_turns=10)) == 1
    assert sdk.calls == trace.model_calls == (0 if elapsed == 119.5 else 1)
    if sdk.options:
        assert sdk.options[0]["timeout"] == min(10, 119 - elapsed)
        assert sdk.options[0]["max_retries"] == 0
    assert clock.now < 120
    assert trace.outcome == "answered"
    assert execution.CURRENT.get() is None


def test_optional_budget_does_not_extend_overall_deadline(clock):
    with request_policy(
        _settings(agent_request_deadline_seconds=2), "openai"
    ) as parent:
        with pytest.raises(ExecutionFailure, match="request_timeout"):
            with execution.optional_model_budget(10):
                # A synchronous transport may ignore its timeout; do not deliver late.
                clock.now = 3
        assert execution.CURRENT.get() is parent
        with pytest.raises(ExecutionFailure, match="request_timeout"):
            execution.checkpoint()


@pytest.mark.parametrize(
    "code",
    ["provider_timeout", "provider_failure", "request_timeout", "invalid_response"],
)
def test_question_evaluator_records_execution_failure_and_continues(
    code, monkeypatch, tmp_path
):
    import sys

    from scripts import evaluate_agent_questions as evaluator

    calls = []

    class EvaluationAgent:
        def __init__(self, *args):
            pass

        def answer(self, question, **kwargs):
            calls.append(question)
            if len(calls) == 1:
                raise ExecutionFailure(code) from RuntimeError("private provider body")
            return {
                "answer": "Synthetic answer",
                **{
                    key: []
                    for key in (
                        "assumptions",
                        "tables",
                        "charts",
                        "metric_definitions",
                        "followups",
                    )
                },
            }

    monkeypatch.setattr(evaluator, "get_settings", _settings)
    monkeypatch.setattr(evaluator, "BigQueryWarehouseRepository", lambda settings: None)
    monkeypatch.setattr(evaluator, "StatsAgent", EvaluationAgent)
    monkeypatch.setattr(
        evaluator,
        "_load_cases",
        lambda path: [
            {"id": "first", "question": "First"},
            {"id": "second", "question": "Second"},
        ],
    )
    monkeypatch.setattr(
        sys, "argv", ["evaluate_agent_questions.py", "--report-dir", str(tmp_path)]
    )
    assert evaluator.main() == 1
    assert calls == ["First", "Second"]
    report_text = next(tmp_path.glob("*.json")).read_text()
    report = json.loads(report_text)
    assert report["status"] == "failed"
    assert report["cases"][0]["errors"] == [code]
    assert report["cases"][1]["errors"] == []
    assert "private provider body" not in report_text


@pytest.mark.parametrize("mutation", ["clear", "set"])
def test_pending_clarification_reads_staged_changes_and_rolls_back_failed_request(
    mutation,
):
    from app.agent.conversation import InMemoryConversationStore

    store = InMemoryConversationStore()
    other_store = InMemoryConversationStore()
    store.set_pending_clarification("same", question="Original", query_plan=None)
    store.set_pending_clarification("other", question="Other chat", query_plan=None)
    other_store.set_pending_clarification(
        "same", question="Other season", query_plan=None
    )
    settings = _settings()
    with pytest.raises(ExecutionFailure, match="provider_failure"):
        with request_policy(settings, "openai"):
            if mutation == "clear":
                store.clear_pending_clarification("same")
                assert store.get_pending_clarification("same") is None
            else:
                store.set_pending_clarification(
                    "same", question="Replacement", query_plan={"metric": "ast"}
                )
                staged = store.get_pending_clarification("same")
                assert staged.question == "Replacement"
                assert staged.query_plan == {"metric": "ast"}
            assert store.get_pending_clarification("other").question == "Other chat"
            assert (
                other_store.get_pending_clarification("same").question == "Other season"
            )
            # A separate request must see persisted state, not this request's
            # staged clear/set. Returning restores the original request overlay.
            with request_policy(settings, "openai"):
                assert store.get_pending_clarification("same").question == "Original"
                store.set_pending_clarification(
                    "same", question="Another request", query_plan=None
                )
                assert (
                    store.get_pending_clarification("same").question
                    == "Another request"
                )
            restored = store.get_pending_clarification("same")
            assert (
                restored is None
                if mutation == "clear"
                else restored.question == "Replacement"
            )
            raise ExecutionFailure("provider_failure")
    assert store.get_pending_clarification("same").question == "Original"
    assert store.get_pending_clarification("other").question == "Other chat"
    assert other_store.get_pending_clarification("same").question == "Other season"
    assert store.get_turns("same", max_turns=10) == []


@pytest.mark.parametrize("mutation", ["clear", "set"])
@pytest.mark.parametrize("expires_before_finalization", [False, True])
def test_finalization_deadline_cannot_half_commit_pending_and_answer(
    mutation, expires_before_finalization, clock
):
    from app.agent.conversation import InMemoryConversationStore
    from app.agent.execution import defer_until_success, governed_request

    store = InMemoryConversationStore()
    store.set_pending_clarification("commit", question="Original", query_plan=None)

    class GovernedAnswer:
        settings = _settings(agent_request_deadline_seconds=1)

        @governed_request
        def answer(self):
            if mutation == "clear":
                store.clear_pending_clarification("commit")
                assert store.get_pending_clarification("commit") is None
            else:
                store.set_pending_clarification(
                    "commit", question="Replacement", query_plan={"metric": "ast"}
                )
                assert (
                    store.get_pending_clarification("commit").question == "Replacement"
                )
            # Expire between the pending mutation and turn commit. The request
            # crossed its final success checkpoint before callbacks began.
            assert defer_until_success(lambda: setattr(clock, "now", 2))
            store.append_turn(
                "commit", question="Answered", answer="Verified answer", max_turns=10
            )
            clock.now = 2 if expires_before_finalization else 0.9
            return {"status": "ok"}

    if expires_before_finalization:
        with pytest.raises(ExecutionFailure, match="request_timeout"):
            GovernedAnswer().answer()
        assert store.get_pending_clarification("commit").question == "Original"
        assert store.get_turns("commit", max_turns=10) == []
    else:
        assert GovernedAnswer().answer() == {"status": "ok"}
        assert clock.now == 2
        pending = store.get_pending_clarification("commit")
        assert (
            pending is None
            if mutation == "clear"
            else pending.question == "Replacement"
        )
        turns = store.get_turns("commit", max_turns=10)
        assert [(turn.question, turn.answer) for turn in turns] == [
            ("Answered", "Verified answer")
        ]


@pytest.mark.parametrize(
    "name", ["research_scope", "research_intent_v2", "research_intent_v3"]
)
def test_research_contract_failures_keep_planning_classification(name):
    observed_stages = []

    def invalid(**kwargs):
        observed_stages.append(execution.ACTIVE_STAGES.get()[-1])
        raise ValueError("Synthetic invalid plan")

    client = NS(responses=NS(create=invalid))
    with pytest.raises(ExecutionFailure) as raised:
        create_response(client, settings=_settings(), text={"format": {"name": name}})
    assert raised.value.code == "invalid_plan"
    assert observed_stages == ["planning"]


def test_exclusive_stages_partition_nested_work_even_on_failure(clock):
    trace = AgentTrace("test", "", "test")
    with request_policy(_settings(), "openai", trace):
        with stage("comparison"):
            clock.now += 0.1
            with stage("overview"):
                clock.now += 0.2
                with stage("overview"):
                    clock.now += 0.3
            with pytest.raises(ValueError):
                with stage("evidence"):
                    clock.now += 0.4
                    raise ValueError("fixture failure")
    assert trace.stage_exclusive_latency_ms == pytest.approx(
        {"comparison": 100, "overview": 500, "evidence": 400}
    )
    assert sum(trace.stage_exclusive_latency_ms.values()) == pytest.approx(1000)
    assert trace.stage_latency_ms["comparison"] == pytest.approx(1000)
    assert execution.STAGE_CHILDREN.get() is None
    assert execution.ACTIVE_STAGES.get() == ()
    assert "stage_exclusive_latency_ms" in trace.to_log_dict()
