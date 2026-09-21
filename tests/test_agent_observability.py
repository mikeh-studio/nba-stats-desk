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
