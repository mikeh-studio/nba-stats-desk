"""Current-only offline conformance; no real source/identity approval implied."""

import copy
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from app.agent import claims
from app.agent.snapshot_source import CurrentPTSAskShadow, HistoricalPTSAskShadow
from app.serving import FactReadStatus, ServingError, SnapshotHandle
from app.serving.comparison import CurrentPTSComparisonShadow, PTSComparisonRequest
from app.serving.context import PTSShadowRequest, ReadContext
from app.serving.contract import canonical_json, content_digest
from app.serving.current import (
    DEPENDENCIES,
    CurrentReadContext,
    CurrentReadPolicy,
    CurrentSnapshotHandle,
)
from app.serving.player import CurrentPlayerPTSShadow

from shared_serving_fixtures import row, write_artifact

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)


def write_current(root):
    rows = [row("g1", 0), row("g2", 10), row("g3", None), row("g1", 20, player_id=2)]
    rows = [{**r, "season": "2026-27", "game_date": "2026-10-22"} for r in rows]
    rows.sort(key=lambda r: (r["season"], r["phase"], r["game_id"], r["player_id"]))

    def convert(m):
        m["schema"] = "nba_current_pts_snapshot/1"
        m["scope"].update(
            season="2026-27",
            start_date="2026-10-22",
            end_date="2026-10-22",
            as_of="2026-10-30T00:00:00+00:00",
        )
        e = {
            "schema": "current-pts-freshness-evidence/1",
            "provider": "espn",
            "policy_fingerprint": content_digest("synthetic-policy"),
            "checked_at": m["scope"]["as_of"],
            "source_captured_at": "2026-10-23T00:00:00+00:00",
            "discovered_at": "2026-10-29T00:00:00+00:00",
            "revalidated_at": None,
            "warning_after": "2026-11-06T00:00:00+00:00",
            "fresh_through": "2026-11-02T04:00:00+00:00",
            "first_stale_at": "2026-11-02T04:00:00.000001+00:00",
            "reconciliation_id": content_digest("synthetic-rec"),
            "expected_inventory_id": content_digest("synthetic-schedule"),
            "observation_ids_sha256": content_digest("synthetic-observations"),
            "conflict_history_completeness": "caller_pinned_ledger_only",
            "pdf_corroboration": "not_required_not_certified",
        }
        m["lineage"] = {key: content_digest(key) for key in DEPENDENCIES}
        m["lineage"]["freshness_evidence_id"] = content_digest(e)
        m["dependency_ids"] = sorted(
            set(m["lineage"].values())
            | {e["reconciliation_id"], e["expected_inventory_id"]}
        )
        m["current_evidence"] = e
        m["freshness_policy_id"] = "current-espn-weekly/1"
        m["clocks"] = {
            k: e[k] for k in ("source_captured_at", "discovered_at", "revalidated_at")
        }
        m["clocks"].update(reviewed_at=e["checked_at"], hard_expiry=e["first_stale_at"])
        for shard in m["shards"]:
            shard.update(min_date="2026-10-22", max_date="2026-10-22")

    return write_artifact(root, rows=rows, transform=convert)


@pytest.fixture
def current(tmp_path):
    root = tmp_path.resolve() / "current"
    manifest = write_current(root)
    status = FactReadStatus(
        content_digest("status"),
        NOW,
        NOW + timedelta(hours=1),
        frozenset({manifest["snapshot_id"]}),
        {k: "active" for k in manifest["dependency_ids"]},
    )
    live = {"status": status, "now": NOW}
    policy = CurrentReadPolicy(
        lambda: live["status"],
        lambda: live["now"],
        freshness_policy_fingerprint=manifest["current_evidence"]["policy_fingerprint"],
        source_identity_authority_id=manifest["lineage"][
            "source_identity_authority_id"
        ],
    )
    handle = CurrentSnapshotHandle.open(root, policy)
    context = CurrentReadContext.bind(
        handle, expected_snapshot_id=manifest["snapshot_id"], scope=manifest["scope"]
    )
    return root, manifest, live, policy, handle, context


def test_current_ask_sse_player_and_comparison_share_scope_and_claims(current):
    _, m, _, _, _, context = current
    request = PTSShadowRequest.create(1, "average", scope=m["scope"])
    ask = CurrentPTSAskShadow(context)
    data = json.loads(ask.answer_json(request))
    streamed = json.loads(next(ask.answer_sse(request)).split("data: ", 1)[1])
    player = CurrentPlayerPTSShadow(context).get_pts(request)
    pair = CurrentPTSComparisonShadow(context).answer(
        PTSComparisonRequest.create([1, 2], "average", scope=m["scope"])
    )
    assert data == streamed
    assert data["semantic_evidence"] == player["semantic_evidence"]
    assert data["tables"][0]["rows"] == [[1, 5.0, 3, 2, 1]]
    assert pair["tables"][0]["rows"] == [[1, 5.0, 3, 2, 1], [2, 20.0, 1, 1, 0]]
    for p in (data, player, pair):
        assert p["binding"] == context.binding
        assert p["schema"].startswith("current-pts-")
        assert claims.validate(p)["status"] == "complete"
        assert p["runtime_eligible"] is p["cohort_complete"] is False
    assert data["semantic_evidence"]["current_evidence"]["provider"] == "espn"


def test_historical_readers_and_contexts_still_refuse_current(current):
    root, m, _, policy, handle, context = current
    for operation in (
        lambda: SnapshotHandle.open(root, policy),
        lambda: ReadContext.bind(
            handle, expected_snapshot_id=m["snapshot_id"], scope=m["scope"]
        ),
        lambda: HistoricalPTSAskShadow(context),
    ):
        with pytest.raises(ServingError) as caught:
            operation()
        assert caught.value.code in {"unsupported_version", "invalid_artifact"}


def test_status_renewal_cannot_extend_frozen_source_deadline(current):
    _, m, live, _, handle, _ = current
    through = datetime.fromisoformat(m["current_evidence"]["fresh_through"])
    for instant in (through, through + timedelta(microseconds=1)):
        live["now"] = instant
        live["status"] = replace(
            live["status"], checked_at=instant, valid_until=instant + timedelta(hours=1)
        )
        if instant == through:
            assert handle.diagnostic(1, "total")["value"] == 10
        else:
            with pytest.raises(ServingError, match="expired_artifact"):
                handle.read()


@pytest.mark.parametrize(
    "change", ["policy", "source_authority", "scope", "dependency", "evidence"]
)
def test_independent_authority_and_bindings_refuse(current, change):
    root, m, live, policy, handle, context = current
    if change == "policy":
        policy = replace(
            policy, freshness_policy_fingerprint=content_digest("different")
        )
    elif change == "source_authority":
        policy = replace(
            policy, source_identity_authority_id=content_digest("different")
        )
    elif change == "scope":
        with pytest.raises(ServingError):
            context.observe(
                PTSShadowRequest.create(
                    1, "total", scope={**m["scope"], "season": "2025-26"}
                )
            )
        return
    elif change == "dependency":
        live["status"] = replace(live["status"], dependency_states={})
    else:
        bad = copy.deepcopy(m)
        bad["current_evidence"]["first_stale_at"] = "2027-01-01T00:00:00+00:00"
        (root / "manifest.json").write_bytes(canonical_json(bad))
    with pytest.raises(ServingError):
        CurrentSnapshotHandle.open(root, policy)


def test_expiration_and_authority_revocation_apply_at_terminal_emission(current):
    _, m, live, _, _, context = current
    stream = CurrentPTSAskShadow(context).answer_sse(
        PTSShadowRequest.create(1, "total", scope=m["scope"])
    )
    live["status"] = replace(live["status"], allowed_snapshot_ids=frozenset())
    with pytest.raises(ServingError, match="snapshot_not_allowed"):
        next(stream)


def test_current_manifest_and_shards_match_their_published_schema(current):
    from pathlib import Path

    from jsonschema import Draft202012Validator, FormatChecker

    root, manifest, *_ = current
    schema = json.loads(
        Path("contracts/shared-serving/current-snapshot-v1.schema.json").read_bytes()
    )
    historical = json.loads(
        Path("contracts/shared-serving/snapshot-v1.schema.json").read_bytes()
    )
    assert schema["$id"] != historical["$id"]
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(manifest)
    shard = {"$ref": "#/$defs/row", "$defs": schema["$defs"]}
    validator = Draft202012Validator(shard, format_checker=FormatChecker())
    for fact in json.loads((root / "facts/000000.json").read_bytes()):
        validator.validate(fact)
