"""Historical shadow consumer contracts; synthetic data and no active app."""

import copy
import json
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta

import pytest
from app.agent import claims
from app.agent.snapshot_source import HistoricalPTSAskShadow
from app.serving import ServingError, SnapshotHandle
from app.serving.context import PTSShadowRequest, ReadContext
from app.serving.contract import canonical_json
from app.serving.player import PlayerPTSShadow

from shared_serving_fixtures import artifact as artifact
from shared_serving_fixtures import row, write_artifact


@pytest.fixture
def shadow(artifact):
    root, manifest, live, policy = artifact
    handle = SnapshotHandle.open(root, policy)
    context = ReadContext.bind(
        handle, expected_snapshot_id=manifest["snapshot_id"], scope=manifest["scope"]
    )
    request = PTSShadowRequest.create(1, "average", scope=manifest["scope"])
    return context, request, live, root, manifest


def test_ask_json_sse_and_player_use_same_factual_binding(shadow):
    context, request, _, _, _ = shadow
    ask = HistoricalPTSAskShadow(context)
    parsed = json.loads(ask.answer_json(request))
    streamed = json.loads(next(ask.answer_sse(request)).split("data: ", 1)[1])
    player = PlayerPTSShadow(context).get_pts(request)
    assert parsed == streamed
    assert parsed["semantic_evidence"] == player["semantic_evidence"]
    assert parsed["binding"] == player["binding"] == context.binding
    assert parsed["tables"][0]["rows"] == [[1, 5.0, 3, 2, 1]]
    profile = player["player_profile"]
    assert profile["total_pts"] == 10 and profile["average_pts"] == 5
    assert len(profile["game_log"]) == 3
    for payload in (parsed, streamed, player):
        assert claims.validate(payload)["status"] == "complete"
        assert payload["runtime_eligible"] is payload["cohort_complete"] is False
        assert payload["identity_mode"] == "canonical_id_only"
        assert "player_name" not in json.dumps(payload)
        assert not any(k in payload for k in ("rank", "percentile", "data_through"))


@pytest.mark.parametrize("consumer", ["ask", "player"])
def test_visible_numbers_labels_and_membership_cannot_drift(shadow, consumer):
    context, request, *_ = shadow
    payload = (
        HistoricalPTSAskShadow(context).answer(request)
        if consumer == "ask"
        else PlayerPTSShadow(context).get_pts(request)
    )
    bad = copy.deepcopy(payload)
    if consumer == "ask":
        bad["tables"][0]["rows"][0][1] = 900.0
    else:
        bad["player_profile"]["game_log"][0]["game_id"] = "another-game"
    assert claims.validate(bad)["status"] == "invalid"
    if consumer == "ask":
        bad = copy.deepcopy(payload)
        bad["tables"][0]["columns"][1]["label"] = "League rank"
        assert claims.validate(bad)["status"] == "invalid"
    bad = copy.deepcopy(payload)
    bad["semantic_evidence"]["binding"]["snapshot_id"] = "0" * 64
    assert claims.validate(bad)["status"] == "invalid"


@pytest.mark.parametrize(
    "change",
    [
        {"player_id": True},
        {"player_id": 0},
        {"player_id": 999},
        {"metric_id": "dreb"},
        {"aggregation": "rank"},
        {"scope_bytes": canonical_json({"season": "2026-27"})},
        {"scope_bytes": canonical_json({"last_n": 5})},
    ],
)
def test_unsupported_requests_do_not_substitute_scope_or_zero(shadow, change):
    context, request, *_ = shadow
    with pytest.raises(ServingError):
        HistoricalPTSAskShadow(context).answer(replace(request, **change))


@pytest.mark.parametrize("revoke", ["allowlist", "nested", "status_expiry"])
def test_already_bound_consumers_recheck_authority(shadow, revoke):
    context, request, live, root, manifest = shadow
    adapter = HistoricalPTSAskShadow(context)
    stream = adapter.answer_sse(request)
    PlayerPTSShadow(context).get_pts(request)
    if revoke == "allowlist":
        live["status"] = replace(live["status"], allowed_snapshot_ids=frozenset())
    elif revoke == "nested":
        states = dict(live["status"].dependency_states)
        states[manifest["lineage"]["final_capability_id"]] = "revoked"
        live["status"] = replace(live["status"], dependency_states=states)
    elif revoke == "status_expiry":
        live["now"] += timedelta(hours=1)
    for operation in (
        lambda: next(stream),
        lambda: adapter.answer_json(request),
        lambda: PlayerPTSShadow(context).get_pts(request),
    ):
        with pytest.raises(ServingError):
            operation()


def test_revocation_during_render_blocks_output(shadow, monkeypatch):
    import app.agent.snapshot_source as module

    context, request, live, *_ = shadow
    original = module.render

    def render_and_revoke(*args):
        payload = original(*args)
        live["status"] = replace(live["status"], allowed_snapshot_ids=frozenset())
        return payload

    monkeypatch.setattr(module, "render", render_and_revoke)
    with pytest.raises(ServingError, match="snapshot_not_allowed"):
        HistoricalPTSAskShadow(context).answer_json(request)


def test_context_scope_is_immutable_and_returned_copies_do_not_alias(shadow):
    context, request, _, _, manifest = shadow
    binding = context.binding
    binding["scope"]["season"] = "2026-27"
    assert context.binding["scope"] == manifest["scope"]
    with pytest.raises(FrozenInstanceError):
        context._binding_bytes = b"{}"
    payload = PlayerPTSShadow(context).get_pts(request)
    payload["player_profile"]["game_log"][0]["pts"] = 888
    assert (
        PlayerPTSShadow(context).get_pts(request)["player_profile"]["game_log"][0][
            "pts"
        ]
        == 0
    )


def test_snapshot_switch_during_request_refuses(shadow, monkeypatch):
    import app.agent.snapshot_source as module

    context, request, _, root, _ = shadow
    original = module.render

    def replace_artifact(*args):
        payload = original(*args)
        write_artifact(root, [row("g1", 99)])
        return payload

    monkeypatch.setattr(module, "render", replace_artifact)
    with pytest.raises(ServingError):
        HistoricalPTSAskShadow(context).answer_json(request)


@pytest.mark.parametrize("points", [None, 0])
def test_all_null_and_valid_zero_remain_distinct(artifact, points):
    root, _, live, policy = artifact
    manifest = write_artifact(root, [row("g1", points, minutes=0)])
    live["status"] = replace(
        live["status"], allowed_snapshot_ids=frozenset({manifest["snapshot_id"]})
    )
    context = ReadContext.bind(
        SnapshotHandle.open(root, policy),
        expected_snapshot_id=manifest["snapshot_id"],
        scope=manifest["scope"],
    )
    result = PlayerPTSShadow(context).get_pts(
        PTSShadowRequest.create(1, "total", scope=manifest["scope"])
    )
    assert result["player_profile"]["total_pts"] == points
    assert result["player_profile"]["observed_games"] == 1
    assert result["player_profile"]["valid_games"] == int(points is not None)


def test_game_time_team_changes_do_not_change_canonical_player(artifact):
    root, _, live, policy = artifact
    manifest = write_artifact(
        root,
        [
            row("g1", 0, team_id="old"),
            row("g2", 10, team_id="new"),
            row("g3", 30, player_id=2),
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
    result = context.observe(
        PTSShadowRequest.create(1, "total", scope=manifest["scope"])
    )
    assert result["aggregates"]["total"]["value"] == 10
    assert {r["game_id"] for r in result["games"]} == {"g1", "g2"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("binding", {"scope": {"season": "2026-27"}}),
        ("cohort_complete", True),
        ("runtime_eligible", True),
        ("limitations", ["complete_through_2026_10_31"]),
        ("authority", {"status_id": "forged"}),
    ],
)
def test_shadow_metadata_cannot_disagree_with_bound_evidence(shadow, field, value):
    context, request, *_ = shadow
    payload = HistoricalPTSAskShadow(context).answer(request)
    payload[field] = value
    assert claims.validate(payload)["status"] == "invalid"


@pytest.mark.parametrize("mode", ["total", "average"])
def test_each_ask_aggregation_has_correct_json_and_sse_labels(shadow, mode):
    context, request, *_ = shadow
    request = replace(request, aggregation=mode)
    ask = HistoricalPTSAskShadow(context)
    for p in (
        json.loads(ask.answer_json(request)),
        json.loads(next(ask.answer_sse(request)).split("data: ", 1)[1]),
    ):
        assert p["tables"][0]["rows"][0][1] == (10.0 if mode == "total" else 5.0)
        assert p["tables"][0]["columns"][1]["label"] == (
            "Points total" if mode == "total" else "Points per valid appearance"
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("snapshot_id", "wrong-snapshot"),
        ("scope", {"season": "2026-27"}),
        ("player_id", 999),
        ("cohort_complete", True),
        ("runtime_eligible", True),
    ],
)
def test_nested_aggregates_cannot_add_another_factual_binding(shadow, field, value):
    from app.serving.presentation import render

    context, request, *_ = shadow
    evidence = context.observe(request)
    evidence["aggregates"]["total"][field] = value
    with pytest.raises(ServingError, match="claim_binding_mismatch"):
        render(evidence, "player", context.authorize())


def test_bound_context_expires_on_hard_deadline_inside_status_lease(artifact):
    root, _, live, policy = artifact
    deadline = live["now"] + timedelta(minutes=10)
    manifest = write_artifact(
        root, transform=lambda m: m["clocks"].update(hard_expiry=deadline.isoformat())
    )
    live["status"] = replace(
        live["status"], allowed_snapshot_ids=frozenset({manifest["snapshot_id"]})
    )
    context = ReadContext.bind(
        SnapshotHandle.open(root, policy),
        expected_snapshot_id=manifest["snapshot_id"],
        scope=manifest["scope"],
    )
    request = PTSShadowRequest.create(1, "average", scope=manifest["scope"])
    HistoricalPTSAskShadow(context).answer(request)
    live["now"] = deadline
    with pytest.raises(ServingError, match="expired_artifact"):
        PlayerPTSShadow(context).get_pts(request)


def test_moving_clock_payloads_share_facts_without_equal_check_times(shadow):
    context, request, live, *_ = shadow
    ask = HistoricalPTSAskShadow(context).answer(request)
    live["now"] += timedelta(seconds=1)
    player = PlayerPTSShadow(context).get_pts(request)
    a, b = (copy.deepcopy(p["semantic_evidence"]) for p in (ask, player))
    assert a.pop("authority") != b.pop("authority")
    assert a == b


def test_allowed_status_rotation_during_render_refuses_stale_receipt(
    shadow, monkeypatch
):
    import app.agent.snapshot_source as module

    context, request, live, *_ = shadow
    original = module.render

    def rotated(*args):
        p = original(*args)
        live["status"] = replace(live["status"], status_id="f" * 64)
        return p

    monkeypatch.setattr(module, "render", rotated)
    with pytest.raises(ServingError, match="authority_changed"):
        HistoricalPTSAskShadow(context).answer(request)


def test_each_output_has_a_bounded_full_verification_cost(shadow, monkeypatch):
    context, request, *_ = shadow
    original = context._handle._verified
    calls = []

    def count():
        calls.append(1)
        return original()

    monkeypatch.setattr(context._handle, "_verified", count)
    ask = HistoricalPTSAskShadow(context)
    for operation, budget in (
        (lambda: ask.answer_json(request), 5),
        (lambda: list(ask.answer_sse(request)), 6),
        (lambda: PlayerPTSShadow(context).get_pts(request), 4),
    ):
        calls.clear()
        operation()
        assert len(calls) <= budget


@pytest.mark.parametrize(
    "mutation",
    ["extra_binding", "runtime_capability", "extra_evidence", "clock_disclosure"],
)
def test_rehashed_shadow_evidence_still_obeys_closed_contract(shadow, mutation):
    from app.serving.presentation import render

    context, request, *_ = shadow
    e = context.observe(request)
    if mutation == "extra_binding":
        e["binding"]["cohort_complete"] = True
    elif mutation == "runtime_capability":
        e["binding"]["capabilities"]["runtime_eligible"] = True
    elif mutation == "extra_evidence":
        e["rank"] = 1
    else:
        e["evidence_clocks"]["source_captured_at"] = None
    from app.serving.contract import content_digest

    e["binding"]["context_id"] = content_digest(
        {k: v for k, v in e["binding"].items() if k != "context_id"}
    )
    with pytest.raises(ServingError, match="claim_binding_mismatch"):
        render(e, "player", context.authorize())


def test_bundle_operation_rechecks_permission_after_arithmetic(artifact, monkeypatch):
    import app.serving.snapshot as module

    root, manifest, live, policy = artifact
    handle = SnapshotHandle.open(root, policy)
    original = module._pts_aggregate

    def revoked(*args):
        result = original(*args)
        live["status"] = replace(live["status"], allowed_snapshot_ids=frozenset())
        return result

    monkeypatch.setattr(module, "_pts_aggregate", revoked)
    with pytest.raises(ServingError, match="snapshot_not_allowed"):
        handle.observed_pts_bundle(1, scope=manifest["scope"])


@pytest.mark.parametrize(
    "mutation", ["game_binding", "player_labels", "typed_capability"]
)
def test_final_nested_shadow_fields_are_closed(shadow, mutation):
    from app.serving.contract import content_digest
    from app.serving.presentation import render

    context, request, *_ = shadow
    e = context.observe(request)
    if mutation == "game_binding":
        e["games"][0]["player_id"] = 999
    elif mutation == "typed_capability":
        e["binding"]["capabilities"]["observed_sample"] = 1
        e["binding"]["context_id"] = content_digest(
            {k: v for k, v in e["binding"].items() if k != "context_id"}
        )
    if mutation == "player_labels":
        p = render(e, "player", context.authorize())
        p["semantic_evidence"]["labels"] = [{"label": "League rank"}]
        p["claim_bindings"]["evidence_sha256"]["semantic_evidence"] = claims.digest(
            p["semantic_evidence"]
        )
        assert claims.validate(p)["status"] == "invalid"
    else:
        with pytest.raises(ServingError, match="claim_binding_mismatch"):
            render(e, "player", context.authorize())
