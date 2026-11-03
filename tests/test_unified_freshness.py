"""Weekly freshness is based on original upstream clocks, never replay clocks."""

import copy
import hashlib

import pytest
from app.data.unified.evaluation import (
    coverage,
    expected_inventory,
    metric_catalog,
    reconciliation,
)
from app.data.unified.freshness import (
    assess,
    correction_reviews,
    next_weekly_refresh,
    revalidation_times,
)
from app.data.unified.ledger import record
from app.data.unified.selection import timestamp
from tests.support.historical import receipt

from test_unified_evaluation import authority, query
from test_unified_pdf_checks import fixture


def discovery(inputs, captured="2026-10-29T00:00:00+00:00"):
    value = copy.deepcopy(authority(inputs)["payload"])
    body = b"Synthetic upstream scope discovery, not real NBA evidence"
    value["authority"]["discovery"] = {
        "body": body.decode(),
        "receipt": receipt(body, captured),
        "inventory_sha256": value["authority"]["attested_inventory_sha256"],
    }
    return expected_inventory(value["scope"], value)


def test_fresh_complete_current_scope_can_qualify_offline_only(tmp_path):
    _, _, chosen, inputs, _, _ = fixture(tmp_path)
    rec = reconciliation(chosen, inputs, discovery(inputs))
    assert rec["payload"]["current_freshness"] == "fresh"
    result = coverage([rec], metric_catalog(), [query()])
    assert result["payload"]["metrics"][0]["league_rank_eligible"]
    assert not result["payload"]["runtime_eligible"]


def test_review_clock_does_not_refresh_discovery_and_warning_is_nonblocking(tmp_path):
    _, _, _, inputs, _, _ = fixture(tmp_path)
    s, obs = inputs["payload"]["scope"], inputs["payload"]["observations"]
    asof = timestamp("2026-10-30T06:00:00+00:00")
    assert assess(s, authority(inputs), obs, [], asof)[0] == "not_established"
    warn = discovery(inputs, "2026-10-21T00:00:00+00:00")
    assert assess(s, warn, obs, [], asof) == (
        "fresh",
        ["upstream_discovery_age_warning"],
    )
    old = discovery(inputs, "2026-10-19T00:00:00+00:00")
    assert "upstream_discovery_stale" in assess(s, old, obs, [], asof)[1]


def test_sunday_eight_pacific_and_dst_are_explicit():
    assert (
        next_weekly_refresh(timestamp("2026-10-30T12:00:00+00:00")).isoformat()
        == "2026-11-01T16:00:00+00:00"
    )
    assert (
        next_weekly_refresh(timestamp("2026-10-25T15:00:00+00:00")).isoformat()
        == "2026-10-25T15:00:00+00:00"
    )
    assert (
        next_weekly_refresh(timestamp("2026-10-25T15:00:01+00:00")).isoformat()
        == "2026-11-01T16:00:00+00:00"
    )


def test_correction_milestones_carry_forward_and_post_final_hold(tmp_path):
    _, _, _, inputs, _, pdf = fixture(tmp_path)
    obs = inputs["payload"]["observations"]
    asof = timestamp("2026-12-10T00:00:00+00:00")
    s = {**inputs["payload"]["scope"], "last": "2026-12-09"}
    proof = discovery(inputs, "2026-12-09T00:00:00+00:00")
    assert "correction_milestone_overdue" in assess(s, proof, obs, [], asof)[1]
    check = copy.deepcopy(pdf["payload"]["pbp_sources"][0])
    check["receipt"]["captured_at"] = "2026-11-23T00:00:00+00:00"
    assert assess(s, proof, obs, [check], asof) == ("fresh", [])
    assert (
        "unresolved_after_final_milestone_needs_review"
        in assess(s, proof, obs, [check], asof, True)[1]
    )


@pytest.mark.parametrize("mode", ["clock", "changed_box", "hash", "duplicate"])
def test_revalidation_must_retain_exact_facts_and_original_capture(tmp_path, mode):
    _, _, _, inputs, _, pdf = fixture(tmp_path)
    checks = copy.deepcopy(pdf["payload"]["pbp_sources"])
    if mode == "clock":
        checks[0]["receipt"]["captured_at"] = "2026-12-01T00:00:00+00:00"
    elif mode == "hash":
        checks[0]["receipt"]["sha256"] = "f" * 64
    elif mode == "duplicate":
        checks *= 2
    else:
        import json

        raw = json.loads(checks[0]["source_body"])
        raw["boxscore"]["players"][0]["statistics"][0]["athletes"][0]["stats"][0] = "25"
        checks[0]["source_body"] = json.dumps(raw)
        checks[0]["receipt"]["sha256"] = hashlib.sha256(
            checks[0]["source_body"].encode()
        ).hexdigest()
    with pytest.raises(ValueError):
        revalidation_times(
            inputs["payload"]["observations"],
            checks,
            timestamp("2026-10-30T06:00:00+00:00"),
        )


def test_discovery_cannot_claim_review_as_capture(tmp_path):
    _, _, chosen, inputs, _, _ = fixture(tmp_path)
    p = copy.deepcopy(discovery(inputs)["payload"])
    p["authority"]["discovery"]["receipt"]["kind"] = "reviewed_identity"
    expected = record("expected_inventory", p)
    with pytest.raises(ValueError, match="source capture"):
        reconciliation(chosen, inputs, expected)


def test_late_component_review_is_scoped_and_cannot_mark_clean_games_unresolved(
    tmp_path,
):
    _, _, _, inputs, _, pdf = fixture(tmp_path)
    obs = inputs["payload"]["observations"]
    check = copy.deepcopy(pdf["payload"]["pbp_sources"][0])
    check["receipt"]["captured_at"] = "2026-11-23T00:00:00+00:00"
    dispute = {
        "observation_id": obs[0]["observation_id"],
        "kind": "component",
        "components": ["ast"],
    }
    clean = copy.deepcopy(obs[0])
    clean["observation_id"] = "f" * 64
    result = correction_reviews(
        [*obs, clean], [check], [dispute], [], timestamp("2026-12-10T00:00:00+00:00")
    )
    assert len(result) == 1 and result[0]["components"] == ["ast"]
    assert result[0]["state"] == "needs_review" and not result[0]["membership_hold"]
    assert "unresolved_after_final_milestone_needs_review" in result[0]["reasons"]


def test_correction_cli_retains_referenced_evidence_and_replays_exactly(
    tmp_path, monkeypatch
):
    import json

    import app.data.unified.artifacts as artifacts
    from scripts import evaluate_unified_coverage as cli

    _, _, chosen, inputs, _, pdf = fixture(tmp_path)
    monkeypatch.setattr(artifacts, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    root = tmp_path / "reports"
    root.mkdir()
    selection = root / "selection.json"
    selection.write_text(json.dumps(chosen))
    checks = copy.deepcopy(pdf["payload"]["pbp_sources"])
    checks[0]["receipt"]["captured_at"] = "2026-10-29T00:00:00+00:00"
    checks[0]["source_path"] = artifacts.retain(
        checks[0]["source_body"].encode(),
        checks[0]["receipt"],
        root / "original-evidence",
        ".body",
    )
    checks[0]["source_body"] = None
    evidence = root / "revalidations.json"
    evidence.write_text(json.dumps(checks))
    first = cli.run(
        tmp_path / "source.sqlite",
        selection,
        root / "ledger.sqlite",
        root / "first",
        [query()],
        revalidations_path=evidence,
    )
    second = cli.run(
        tmp_path / "source.sqlite",
        selection,
        root / "ledger.sqlite",
        root / "replay",
        [query()],
        revalidations_path=evidence,
    )
    assert first["coverage_id"] == second["coverage_id"] and not second["recorded"]
    from app.data.unified.ledger import LedgerStore

    store = LedgerStore(root / "ledger.sqlite")
    covered = store.read(first["coverage_id"])
    rec = store.read(covered["payload"]["reconciliation_ids"][0])
    value = store.read(rec["payload"]["inputs_id"])
    retained = value["payload"]["revalidations"][0]
    assert retained["source_body"] is None and retained["source_path"]
    assert retained["receipt"]["captured_at"] == "2026-10-29T00:00:00+00:00"
    __import__("pathlib").Path(retained["source_path"]).write_text("tampered")
    with pytest.raises(ValueError, match="hash"):
        store.read(first["coverage_id"])
    store.close()
