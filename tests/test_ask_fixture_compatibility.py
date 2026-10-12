"""Baseline capabilities are isolated per case, never silently bypassed."""

import builtins
import copy
import json
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from scripts.ask_eval.fixtures import FixtureAttachments, FixtureCapabilityError
from scripts.evaluate_ask import evaluate

SOURCE = json.loads((Path(__file__).parent / "fixtures/ask/source.json").read_text())


@dataclass(frozen=True)
class OldSettings:
    """Predates availability and its settings field, like the reviewed baseline."""

    agent_teammate_study_path: str | None = None


@dataclass(frozen=True)
class NewSettings(OldSettings):
    research_availability_path: str | None = None


def block_digest_imports(monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name in ("app.agent.semantic_source", "app.research_snapshots"):
            raise ModuleNotFoundError("Simulated old application module")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)


def test_attachment_free_old_settings_never_import_or_write_artifacts(
    tmp_path, monkeypatch
):
    block_digest_imports(monkeypatch)
    artifacts = FixtureAttachments(SOURCE, tmp_path)
    settings = OldSettings()
    assert artifacts.configure(settings, []) is settings
    assert list(tmp_path.iterdir()) == []


def test_old_settings_reject_only_missing_capability_and_can_continue(
    tmp_path, monkeypatch
):
    block_digest_imports(monkeypatch)
    artifacts = FixtureAttachments(SOURCE, tmp_path)
    settings = OldSettings()
    with pytest.raises(FixtureCapabilityError) as error:
        artifacts.configure(settings, ["availability"])
    assert error.value.detail == {
        "code": "evaluation_fixture_capability_unavailable",
        "attachment": "availability",
        "reason": "application_setting_missing",
    }
    with pytest.raises(FixtureCapabilityError):
        artifacts.configure(settings, ["study", "availability"])
    assert list(tmp_path.iterdir()) == []
    assert artifacts.configure(settings, []) is settings
    # Study attachments need neither availability field nor its digest modules.
    configured = artifacts.configure(settings, ["study"])
    assert (
        json.loads(Path(configured.agent_teammate_study_path).read_text())
        == SOURCE["study"]
    )
    assert not hasattr(configured, "research_availability_path")


def test_missing_application_module_is_explicit_and_does_not_poison_other_cases(
    tmp_path, monkeypatch
):
    block_digest_imports(monkeypatch)
    artifacts = FixtureAttachments(SOURCE, tmp_path)
    with pytest.raises(FixtureCapabilityError) as error:
        artifacts.configure(NewSettings(), ["availability"])
    assert error.value.detail["reason"] == "application_attachment_module_missing"
    assert artifacts.paths == {}
    assert artifacts.configure(NewSettings(), ["study"]).agent_teammate_study_path
    assert artifacts.configure(NewSettings(), []).research_availability_path is None


@pytest.mark.parametrize(
    "source", [{}, {"source_kind": "production_snapshot", **SOURCE}]
)
def test_requested_missing_or_non_synthetic_attachment_never_falls_back(
    source, tmp_path
):
    with pytest.raises(FixtureCapabilityError) as error:
        FixtureAttachments(source, tmp_path).configure(NewSettings(), ["availability"])
    assert error.value.detail["reason"] == "frozen_attachment_missing"
    assert list(tmp_path.iterdir()) == []


def test_only_requested_artifact_is_materialized_and_cached(tmp_path):
    artifacts = FixtureAttachments(SOURCE, tmp_path)
    first = artifacts.configure(NewSettings(), ["study"])
    path = Path(first.agent_teammate_study_path)
    modified = path.stat().st_mtime_ns
    second = artifacts.configure(NewSettings(), ["study"])
    assert second == first and path.stat().st_mtime_ns == modified
    assert list(tmp_path.iterdir()) == [path]
    # Attachment settings remain request-local; later cases receive no prior path.
    assert artifacts.configure(NewSettings(), []) == NewSettings()


def test_runner_records_capability_failure_in_both_transports_and_continues(
    monkeypatch,
):
    payload = {
        "detail": {
            "code": "evaluation_fixture_capability_unavailable",
            "attachment": "availability",
            "reason": "application_setting_missing",
        }
    }
    requests = []

    def handle(request):
        requests.append(request)
        if request.headers["X-Evaluation-Case"] == "needs_availability":
            return httpx.Response(501, json=payload)
        if request.url.path.endswith("/stream"):
            return httpx.Response(
                200, text='data: {"type":"final","payload":{"value":5}}\n\n'
            )
        return httpx.Response(200, json={"value": 5})

    actual_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: actual_client(**kwargs, transport=httpx.MockTransport(handle)),
    )
    cases = [
        {
            "id": name,
            "family": "compatibility",
            "turns": [
                {"question": "fixture", "checks": [{"path": "value", "equals": 5}]}
            ],
        }
        for name in ("needs_availability", "ordinary_metrics")
    ]
    original = copy.deepcopy(cases)
    results = evaluate("http://fixture", {"cases": cases})
    assert len(requests) == 4 and cases == original
    failed, unaffected = results
    assert failed["passed"] is False and unaffected["passed"] is True
    for mode in ("json", "sse"):
        turn = failed["modes"][mode][0]
        assert turn["payload"] == payload and turn["http_status"] == 501
        assert turn["checks"][-1] == {
            "path": "HTTP/terminal",
            "passed": False,
            "actual": 501,
        }
