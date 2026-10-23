from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.model_catalog import audit_provider, fetch_catalog, markdown
from scripts.evaluate_agent_questions import _check_case

NOW = "2026-09-05T12:00:00+00:00"


def test_catalog_diff_reports_candidates_missing_and_retirement():
    previous = {"last_success_at": "yesterday", "models": {"gpt-old": {}}}
    models = {
        "gpt-new": {},
        "gpt-retiring": {"shutdown_date": "2026-10-01"},
        "image-model": {},
    }
    result = audit_provider(
        "openai", "test", {"gpt-old"}, previous, NOW, lambda *a: models
    )
    assert result["unavailable"] == ["gpt-old"]
    assert result["unconfigured"] == ["gpt-new", "gpt-retiring"]
    assert result["removed"] == ["gpt-old"]
    assert result["retiring"] == {"gpt-retiring": "2026-10-01"}
    assert result["last_success_at"] == NOW


def test_failure_retains_snapshot_without_claiming_unavailable():
    def fail(*args):
        raise RuntimeError("sensitive request content")

    previous = {"last_success_at": "yesterday", "models": {"gpt-ok": {}}}
    result = audit_provider("openai", "test", {"gpt-ok"}, previous, NOW, fail)
    assert result["status"] == "stale"
    assert result["models"] == previous["models"]
    assert result["last_success_at"] == "yesterday"
    assert "unavailable" not in result
    text = markdown({"checked_at": NOW, "providers": {"openai": result}})
    assert "sensitive" not in text
    assert "Availability is unknown" in text


def test_missing_key_is_unverified_not_empty_success():
    result = audit_provider("claude", "", {"claude-a"}, {}, NOW)
    assert result["status"] == "unverified"
    assert result["error"] == "missing_credentials"


def test_initial_snapshot_does_not_claim_every_model_was_just_released():
    result = audit_provider("openai", "test", set(), {}, NOW, lambda *a: {"gpt-a": {}})
    assert result["baseline"] is True
    assert result["added"] == []
    assert result["unconfigured"] == ["gpt-a"]


def test_retrievable_alias_is_available():
    client = MagicMock()
    client.__enter__.return_value = client
    client.models.list.return_value = iter([SimpleNamespace(id="dated-id")])
    client.models.retrieve.return_value = SimpleNamespace(id="dated-id")
    with patch("anthropic.Anthropic", return_value=client):
        result = fetch_catalog("claude", "test", {"claude-alias"})
    assert result["claude-alias"]["resolved_id"] == "dated-id"
    client.models.retrieve.assert_called_once_with("claude-alias")


def test_retrieval_failure_is_not_confused_with_missing_model():
    client = MagicMock()
    client.__enter__.return_value = client
    client.models.list.return_value = []
    client.models.retrieve.side_effect = RuntimeError("network failure")
    with patch("anthropic.Anthropic", return_value=client):
        result = audit_provider("claude", "test", {"claude-a"}, {}, NOW)
    assert result["status"] == "unverified"
    assert "unavailable" not in result


def test_smoke_check_rejects_missing_tools_and_malformed_structure():
    errors = _check_case(
        {"expected_tools": ["search_rankings"]}, {"answer": "hello", "tables": "bad"}
    )
    assert any("missing expected tool" in error for error in errors)
    assert "invalid structured answer field: tables" in errors


def test_candidate_eval_forwards_provider_and_model_and_preserves_reports(
    tmp_path, monkeypatch
):
    import json

    from scripts import evaluate_agent_questions as evaluator

    fixture = tmp_path / "questions.yml"
    fixture.write_text(
        "- id: test\n  question: Top assists?\n  expected_tools: [search_rankings]\n"
    )
    settings = SimpleNamespace(anthropic_api_key="test", openai_api_key=None)
    monkeypatch.setattr(evaluator, "get_settings", lambda: settings)
    monkeypatch.setattr(
        evaluator, "BigQueryWarehouseRepository", lambda settings: object()
    )
    agent = MagicMock()
    agent.answer.return_value = {
        "answer": "Assists leaders",
        "tool_calls": [{"name": "search_rankings"}],
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
    monkeypatch.setattr(evaluator, "StatsAgent", lambda *args: agent)
    monkeypatch.setattr(
        "sys.argv",
        [
            "eval",
            "--provider",
            "claude",
            "--model",
            "claude-candidate",
            "--fixture",
            str(fixture),
            "--report-dir",
            str(tmp_path / "reports"),
        ],
    )
    assert evaluator.main() == 0
    assert evaluator.main() == 0
    assert agent.answer.call_args.kwargs["provider"] == "claude"
    assert agent.answer.call_args.kwargs["model"] == "claude-candidate"
    reports = list((tmp_path / "reports").glob("*.json"))
    assert len(reports) == 2
    assert json.loads(reports[0].read_text())["status"] == "passed"


def test_openai_ask_default_is_luna(monkeypatch):
    from app.config import OPENAI_AGENT_MODEL_VALUES, Settings, get_settings

    monkeypatch.delenv("OPENAI_AGENT_MODEL", raising=False)
    assert get_settings().openai_agent_model == "gpt-5.6-luna"
    assert Settings.__dataclass_fields__["openai_agent_model"].default == "gpt-5.6-luna"
    assert "gpt-5.6-luna" in OPENAI_AGENT_MODEL_VALUES
    monkeypatch.setenv("OPENAI_AGENT_MODEL", "gpt-5.4-mini")
    assert get_settings().openai_agent_model == "gpt-5.4-mini"
