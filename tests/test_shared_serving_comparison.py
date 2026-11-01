"""Offline comparison invariants; synthetic facts, no runtime selection."""

import copy
import json
from dataclasses import replace

import pytest
from app.agent import claims
from app.serving import ServingError, SnapshotHandle
from app.serving.comparison import HistoricalPTSComparisonShadow, PTSComparisonRequest
from app.serving.context import ReadContext
from app.serving.contract import canonical_json

from shared_serving_fixtures import artifact as artifact
from shared_serving_fixtures import row, write_artifact


@pytest.fixture
def comparison(artifact):
    root, _, live, policy = artifact
    manifest = write_artifact(
        root,
        [
            row("g1", 0),
            row("g1", 30, player_id=2),
            row("g2", None),
            row("g2", 10, player_id=2, team_id="team-b"),
        ],
    )
    live["status"] = replace(
        live["status"], allowed_snapshot_ids=frozenset({manifest["snapshot_id"]})
    )
    context = ReadContext.bind(
        SnapshotHandle.open(root, policy),
        expected_snapshot_id=manifest["snapshot_id"],
        scope=manifest["scope"],
    )
    plan = PTSComparisonRequest.create([1, 2], "average", scope=manifest["scope"])
    return HistoricalPTSComparisonShadow(context), plan, context, live, root, manifest


def test_side_by_side_partial_samples_and_transport_parity(comparison):
    adapter, plan, context, *_ = comparison
    payload = adapter.answer(plan)
    assert payload["tables"][0]["rows"] == [[1, 0.0, 2, 1, 1], [2, 20.0, 2, 2, 0]]
    assert payload["binding"] == context.binding
    assert claims.validate(payload)["status"] == "complete"
    assert json.loads(adapter.answer_json(plan)) == payload
    assert json.loads(next(adapter.answer_sse(plan)).split("data: ", 1)[1]) == payload
    assert "comparison_edges_unavailable" in payload["limitations"]
    assert not any(k in payload for k in ("delta", "winner", "rank", "percentile"))
    sides = payload["semantic_evidence"]["sides"]
    assert sides[1]["games"][1]["team_id"] == "team-b"
    assert sides[0]["aggregates"]["average"]["denominator"] == 1


def test_all_null_observed_member_is_retained_as_unknown(comparison):
    adapter, plan, context, live, root, _ = comparison
    manifest = write_artifact(root, [row("g1", None), row("g1", 0, player_id=2)])
    live["status"] = replace(
        live["status"], allowed_snapshot_ids=frozenset({manifest["snapshot_id"]})
    )
    # New content needs a newly bound handle, not a repointed old context.
    policy = context._handle._policy
    new_context = ReadContext.bind(
        SnapshotHandle.open(root, policy),
        expected_snapshot_id=manifest["snapshot_id"],
        scope=manifest["scope"],
    )
    payload = HistoricalPTSComparisonShadow(new_context).answer(plan)
    assert payload["tables"][0]["rows"] == [[1, None, 1, 0, 1], [2, 0.0, 1, 1, 0]]
    with pytest.raises(ServingError):
        adapter.answer(plan)


@pytest.mark.parametrize("ids", [(1, 999), (999, 2), (1, 1), (True, 2), (1, 2, 3)])
def test_invalid_or_absent_member_refuses_whole_output(comparison, ids):
    adapter, plan, *_ = comparison
    with pytest.raises(ServingError):
        adapter.answer(replace(plan, player_ids=ids))


@pytest.mark.parametrize(
    "change",
    [{"aggregation": "rank"}, {"scope_bytes": canonical_json({"season": "2026-27"})}],
)
def test_unsupported_scope_or_operation_refuses(comparison, change):
    adapter, plan, *_ = comparison
    with pytest.raises(ServingError, match="capability_not_evaluated"):
        adapter.answer(replace(plan, **change))


@pytest.mark.parametrize(
    "mutation", ["number", "label", "binding", "policy", "extra", "nested"]
)
def test_claims_reject_comparison_drift(comparison, mutation):
    adapter, plan, *_ = comparison
    payload = copy.deepcopy(adapter.answer(plan))
    if mutation == "number":
        payload["tables"][0]["rows"][1][1] = 999
    elif mutation == "label":
        payload["tables"][0]["columns"][1]["label"] = "League rank"
    elif mutation == "binding":
        payload["semantic_evidence"]["sides"][1]["binding"] = {"context_id": "x"}
    elif mutation == "policy":
        payload["semantic_evidence"]["policy"] = "infer_winner/1"
    elif mutation == "extra":
        payload["winner"] = 2
    else:
        payload["semantic_evidence"]["sides"][1]["cohort_complete"] = True
    assert claims.validate(payload)["status"] == "invalid"


def test_lazy_sse_rechecks_and_emits_nothing_after_revocation(comparison):
    adapter, plan, _, live, *_ = comparison
    stream = adapter.answer_sse(plan)
    live["status"] = replace(live["status"], allowed_snapshot_ids=frozenset())
    with pytest.raises(ServingError):
        next(stream)


def test_revocation_after_second_member_prevents_combined_output(
    comparison, monkeypatch
):
    adapter, plan, context, live, *_ = comparison
    original = context.observe

    def observe(request):
        value = original(request)
        if request.player_id == 2:
            live["status"] = replace(live["status"], allowed_snapshot_ids=frozenset())
        return value

    monkeypatch.setattr(
        type(context), "observe", lambda self, request: observe(request)
    )
    with pytest.raises(ServingError):
        adapter.answer(plan)


def test_output_budget_refuses_without_partial_table(comparison, monkeypatch):
    import app.serving.comparison as module

    adapter, plan, *_ = comparison
    monkeypatch.setattr(module, "MAX_OUTPUT_BYTES", 1)
    with pytest.raises(ServingError, match="comparison_response_budget"):
        adapter.answer(plan)


@pytest.mark.parametrize("error", [OverflowError, AttributeError, RecursionError])
def test_render_failure_has_bounded_code(comparison, monkeypatch, error):
    import app.serving.comparison as module

    adapter, plan, *_ = comparison

    def fail(*args):
        raise error("synthetic render failure")

    monkeypatch.setattr(module, "_render", fail)
    with pytest.raises(ServingError, match="claim_binding_mismatch"):
        adapter.answer(plan)
