"""Offline two-season contracts use only synthetic facts and caller authority."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from app.serving import FactReadStatus, OfflineReadPolicy, ServingError, composite
from app.serving.composite import CompositionAuthority, TwoSeasonPTS
from app.serving.contract import content_digest
from app.serving.current import CurrentReadPolicy

from shared_serving_fixtures import NOW, row, write_artifact
from test_current_serving_snapshot import write_current


@pytest.fixture
def prepared(tmp_path):
    root = tmp_path.resolve()
    hm = write_artifact(
        root / "historical",
        [row("g1", 0), row("g1", 20, player_id=2), row("g2", 10), row("g3", None)],
    )
    cm = write_current(root / "current")

    def status(m, label):
        return FactReadStatus(
            content_digest(label),
            NOW,
            NOW + timedelta(hours=1),
            frozenset({m["snapshot_id"]}),
            {k: "active" for k in m["dependency_ids"]},
        )

    live = {"h": status(hm, "h"), "c": status(cm, "c")}
    hp = OfflineReadPolicy(lambda: live["h"], lambda: NOW)
    cp = CurrentReadPolicy(
        lambda: live["c"],
        lambda: NOW,
        freshness_policy_fingerprint=cm["current_evidence"]["policy_fingerprint"],
        source_identity_authority_id=cm["lineage"]["source_identity_authority_id"],
    )
    authority = CompositionAuthority(
        composite.POLICY,
        content_digest("identity-review"),
        hm["lineage"]["identity_id"],
        cm["lineage"]["identity_id"],
        frozenset({1, 2, 999}),
    )

    def open_context(auth=authority):
        return TwoSeasonPTS.open(
            root / "historical",
            hp,
            root / "current",
            cp,
            historical_snapshot_id=hm["snapshot_id"],
            current_snapshot_id=cm["snapshot_id"],
            authority=auth,
        )

    return open_context, live, hm, cm, authority


def test_two_seasons_ask_player_comparison_and_sse(prepared):
    make, _, hm, cm, _ = prepared
    ctx = make()
    for consumer in ("ask", "player"):
        result = ctx.answer((1,), "average", consumer=consumer)
        assert [m["binding"]["scope"]["season"] for m in result["members"]] == [
            "2025-26",
            "2026-27",
        ]
        assert [
            m["semantic_evidence"]["aggregates"]["average"]["value"]
            for m in result["members"]
        ] == [5.0, 5.0]
        assert result["runtime_eligible"] is False
    result = ctx.answer((1, 2), "total")
    assert len(result["members"]) == 2
    stream = json.loads(next(ctx.answer_sse((1,), "total")).split("data: ", 1)[1])
    assert stream == json.loads(ctx.answer_json((1,), "total"))
    assert [m["snapshot_id"] for m in ctx.binding["members"]] == [
        hm["snapshot_id"],
        cm["snapshot_id"],
    ]


def test_refuses_unapproved_identity_missing_member_and_expired_status(prepared):
    make, live, _, _, auth = prepared
    with pytest.raises(ServingError, match="identity_compatibility_denied"):
        make(replace(auth, current_identity_id=content_digest("wrong")))
    ctx = make()
    with pytest.raises(ServingError, match="identity_compatibility_denied"):
        ctx.answer((3,), "total")
    with pytest.raises(ServingError, match="player_not_observed"):
        ctx.answer((999,), "total")
    stream = ctx.answer_sse((1,), "total")
    live["c"] = replace(live["c"], allowed_snapshot_ids=frozenset())
    with pytest.raises(ServingError, match="snapshot_not_allowed"):
        next(stream)


def test_combined_budget_checked_before_fact_read(prepared, monkeypatch):
    make, _, _, _, _ = prepared
    from app.serving import snapshot

    original = snapshot._read_at
    facts = []

    def track(fd, relative, limit):
        if relative.startswith("facts/"):
            facts.append(relative)
        return original(fd, relative, limit)

    monkeypatch.setattr(snapshot, "_read_at", track)
    monkeypatch.setattr(composite, "MAX_ROWS", 1)
    with pytest.raises(ServingError, match="budget_exceeded"):
        make()
    assert not facts


def test_unhashable_identity_is_a_bounded_refusal(prepared):
    context = prepared[0]()
    with pytest.raises(ServingError, match="identity_compatibility_denied"):
        context.answer(([1],), "total")


@pytest.mark.parametrize("limit", ["MAX_ROWS", "MAX_BYTES", "MAX_SHARDS"])
def test_all_combined_budgets_precede_facts(prepared, monkeypatch, limit):
    from app.serving import snapshot

    calls = []
    read = snapshot._read_at

    def tracked(fd, relative, bound):
        if relative.startswith("facts/"):
            calls.append(relative)
        return read(fd, relative, bound)

    monkeypatch.setattr(snapshot, "_read_at", tracked)
    monkeypatch.setattr(composite, limit, 1)
    with pytest.raises(ServingError, match="budget_exceeded"):
        prepared[0]()
    assert not calls


def test_output_budget_and_mid_read_revocation(prepared, monkeypatch):
    make, live, *_ = prepared
    ctx = make()
    monkeypatch.setattr(composite, "MAX_OUTPUT", 100)
    with pytest.raises(ServingError, match="budget_exceeded"):
        ctx.answer((1,), "total")
    monkeypatch.setattr(composite, "MAX_OUTPUT", 1024 * 1024)
    first = ctx._contexts[0]
    original = type(first).observe

    def revoke(context, request):
        result = original(context, request)
        if context is first:
            live["c"] = replace(live["c"], allowed_snapshot_ids=frozenset())
        return result

    monkeypatch.setattr(type(first), "observe", revoke)
    with pytest.raises(ServingError, match="snapshot_not_allowed"):
        ctx.answer((1,), "total")


def test_manifest_replacement_refuses(prepared, tmp_path):
    ctx = prepared[0]()
    path = tmp_path.resolve() / "historical/manifest.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ServingError, match="integrity_failure"):
        ctx.answer((1,), "total")


def test_observed_all_null_is_retained_and_one_sided_absence_refuses(
    prepared, tmp_path
):
    import hashlib

    from app.serving.contract import canonical_json

    make, live, _, manifest, _ = prepared
    root = tmp_path.resolve() / "current"
    shard = root / "facts/000000.json"
    rows = json.loads(shard.read_bytes())
    for entry in rows:
        if entry["player_id"] == 1:
            entry["pts"] = None
    data = canonical_json(rows)
    shard.write_bytes(data)
    manifest["shards"][0]["sha256"] = hashlib.sha256(data).hexdigest()
    manifest["shards"][0]["byte_count"] = len(data)
    manifest["snapshot_id"] = content_digest(
        {k: v for k, v in manifest.items() if k != "snapshot_id"}
    )
    (root / "manifest.json").write_bytes(canonical_json(manifest))
    live["c"] = replace(
        live["c"], allowed_snapshot_ids=frozenset({manifest["snapshot_id"]})
    )
    result = make().answer((1,), "average")
    aggregates = result["members"][1]["semantic_evidence"]["aggregates"]["average"]
    assert aggregates["value"] is None and aggregates["observed_games"] == 3
    assert aggregates["valid_games"] == 0 and aggregates["missing_component_games"] == 3
    # Player 2 is present in both; player 999 is absent in both. Remove player 2
    # from only this season, then verify the combined request refuses.
    rows = [r for r in rows if r["player_id"] != 2]
    data = canonical_json(rows)
    shard.write_bytes(data)
    manifest["shards"][0].update(
        sha256=hashlib.sha256(data).hexdigest(),
        byte_count=len(data),
        row_count=len(rows),
    )
    manifest["row_count"] = len(rows)
    manifest["snapshot_id"] = content_digest(
        {k: v for k, v in manifest.items() if k != "snapshot_id"}
    )
    (root / "manifest.json").write_bytes(canonical_json(manifest))
    live["c"] = replace(
        live["c"], allowed_snapshot_ids=frozenset({manifest["snapshot_id"]})
    )
    with pytest.raises(ServingError, match="player_not_observed"):
        make().answer((2,), "total")


@pytest.mark.parametrize("season", ["h", "c"])
def test_status_rotation_during_serialization_refuses(prepared, monkeypatch, season):
    make, live, *_ = prepared
    context = make()
    encoder = composite.bounded_json
    calls = 0

    def rotate(value):
        nonlocal calls
        result = encoder(value)
        calls += 1
        if calls == 2:
            live[season] = replace(live[season], status_id=content_digest("rotated"))
        return result

    monkeypatch.setattr(composite, "bounded_json", rotate)
    with pytest.raises(ServingError, match="authority_changed"):
        context.answer_json((1,), "average")
