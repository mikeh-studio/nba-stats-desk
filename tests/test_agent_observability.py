import json

from app.agent.observability import AgentTrace


def test_summary_omits_content_but_retains_operational_metrics():
    marker = "private-user-content"
    trace = AgentTrace("server-id", marker, "configured-model", conversation_id=marker)
    record = trace.add_tool(
        name=marker,
        args={"query": marker},
        status="ok",
        duration_ms=12,
        result={"message": marker, "rows": [{"text": marker}]},
    )
    trace.set_plan(route="player_trend", confidence=0.9)
    trace.outcome = "answered"
    summary = trace.to_log_dict()
    assert marker not in json.dumps(summary)
    assert summary["total_tool_calls"] == 1
    assert summary["tool_latency_ms"] == 12
    assert summary["outcome"] == "answered"
    assert summary["route"] == "player_trend"
    # Response/local-history detail is preserved separately.
    assert record["args"] == {"query": marker}


def test_planner_fallback_does_not_log_provider_exception_body(caplog):
    from types import SimpleNamespace

    from app.agent.planner import build_query_plan
    from app.config import Settings

    marker = "private-provider-error-body"

    def create(**kwargs):
        raise RuntimeError(marker)

    settings = Settings(
        project_id="test",
        gold_dataset="gold",
        metadata_dataset="metadata",
        freshness_threshold_hours=36,
        max_search_results=10,
    )
    with caplog.at_level("WARNING", logger="nba.agent.planner"):
        plan = build_query_plan(
            "How is Tyrese Maxey trending?",
            settings=settings,
            client=SimpleNamespace(responses=SimpleNamespace(create=create)),
        )
    assert plan.planner_source == "deterministic"
    assert "RuntimeError" in caplog.text
    assert marker not in caplog.text


def test_token_details_preserve_unknown_and_do_not_double_count_output():
    from types import SimpleNamespace as NS

    trace = AgentTrace("id", "private", "model")
    assert trace.to_log_dict()["tokens"]["cached_input"] is None
    trace.add_usage(
        NS(
            input_tokens=100,
            output_tokens=20,
            input_tokens_details=NS(cached_tokens=80),
            output_tokens_details={"reasoning_tokens": 10},
        )
    )
    assert trace.total_tokens == 120
    assert trace.cached_input_tokens == 80
    assert trace.reasoning_output_tokens == 10


def test_cache_does_not_rebill_warehouse_and_metadata_is_private_safe():
    from app.agent.execution import request_policy
    from app.agent.semantic_serving import SemanticWarehouse
    from app.agent.semantic_source import BigQuerySemanticSource
    from tests.test_agent_service import _settings
    from tests.test_semantic_source import Client

    trace = AgentTrace("id", "private", "model")
    warehouse = SemanticWarehouse(
        BigQuerySemanticSource(Client(), project="test-project")
    )
    with request_policy(_settings(), "openai", trace):
        warehouse.load(["2024-25"])
        warehouse.load(["2024-25"])
    jobs = [r for r in trace.data_reads if r["kind"] == "warehouse"]
    cache = [r for r in trace.data_reads if r["kind"] == "evidence_cache"]
    assert len(jobs) == 1 and jobs[0]["bytes_billed"] == 100
    assert [r["cache_hit"] for r in cache] == [False, True]
    assert cache[0]["source_version"] == cache[1]["source_version"]
    assert "validation" in trace.stage_exclusive_latency_ms
    encoded = json.dumps(trace.to_log_dict())
    for private in (
        "private",
        "test-project",
        "SELECT",
        "fixture-job",
        "Regular Player",
    ):
        assert private not in encoded


def test_failed_warehouse_attempt_is_recorded_without_error_body():
    import pytest
    from app.agent.execution import request_policy
    from app.agent.semantic_source import BigQuerySemanticSource
    from app.agent.semantics import SemanticError
    from google.api_core.exceptions import Forbidden
    from tests.test_agent_service import _settings
    from tests.test_semantic_source import Client

    trace = AgentTrace("id", "private", "model")
    with request_policy(_settings(), "openai", trace), pytest.raises(SemanticError):
        BigQuerySemanticSource(
            Client(error=Forbidden("sensitive")), project="test-project"
        ).capture(["2024-25"])
    assert trace.data_reads[0]["status"] == "error"
    assert trace.data_reads[0]["bytes_billed"] is None
    assert "sensitive" not in json.dumps(trace.to_log_dict())


def test_metadata_truncation_is_visible():
    from app.agent.execution import record_data_read, request_policy
    from tests.test_agent_service import _settings

    trace = AgentTrace("id", "private", "model")
    with request_policy(_settings(), "openai", trace):
        for _ in range(65):
            record_data_read(kind="warehouse", status="ok", bytes_billed=1)
    assert len(trace.data_reads) == 64
    assert trace.to_log_dict()["data_reads_dropped"] == 1


def test_cost_telemetry_exposes_usage_and_read_coverage():
    from types import SimpleNamespace

    trace = AgentTrace("id", "private", "model")
    trace.add_usage(None)
    trace.add_usage(SimpleNamespace(input_tokens=2, output_tokens=3))
    log = trace.to_log_dict()
    assert log["usage_reported_calls"] == 1
    assert log["data_read_coverage"] == "semantic_fact_source_only"
    assert log["token_detail_calls"] == {}
