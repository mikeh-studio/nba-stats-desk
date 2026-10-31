"""Regressions for the independent Astra High review findings."""

import copy

import pytest
from app.data.unified.evaluation import coverage, expected_inventory, reconciliation
from app.data.unified.identity import ContractError
from app.data.unified.ledger import LedgerStore, record
from app.data.unified.selection import policy_record, select
from app.data.unified.snapshot import evaluation_snapshot, snapshot
from app.data.unified.store import ObservationStore
from tests.support.historical import complete, ingest, payload

from test_unified_evaluation import (
    AS_OF,
    authority,
    evaluate,
    metric_catalog,
    query,
    request,
    source,
)


def test_crossed_inventory_pins_cannot_bypass_capture_time_guard(tmp_path):
    path = tmp_path / "future.sqlite"
    store = ObservationStore(path)
    for n, day in ((1, "2025-10-22"), (2, "2025-10-23")):
        value = payload("002250000" + str(n), day)
        value["appearances"][2]["ast"] = 0
        ingest(store, complete(value), captured_at="2026-10-31T00:00:00+00:00")
    store.close()
    actual = snapshot(path)
    inputs = evaluation_snapshot(path, actual, request())
    p = copy.deepcopy(actual["payload"])
    for ref in p["observations"]:
        ref["capture_times"] = [t for t in ref["capture_times"] if t < AS_OF]
    suppressed = record("inventory", p)
    policy = policy_record()
    refused = select(actual, policy, [request()], AS_OF)
    chosen = select(suppressed, policy, [request()], AS_OF)
    assert refused["payload"]["status"] == "refused"
    assert chosen["payload"]["status"] == "selected_observed"
    expected = authority(inputs)
    with pytest.raises(ContractError, match="inventory binding"):
        reconciliation(chosen, inputs, expected)
    rec = reconciliation(refused, inputs, expected)
    forged = copy.deepcopy(rec["payload"])
    forged["selection_id"] = chosen["record_id"]
    ledger = LedgerStore()
    for value in (policy, actual, suppressed, inputs, refused, chosen, expected):
        ledger.append(value)
    with pytest.raises(ContractError, match="inventory binding"):
        ledger.append(
            record(
                "reconciliation",
                forged,
                [chosen["record_id"], inputs["record_id"], expected["record_id"]],
            )
        )
    ledger.close()


def test_scope_evidence_is_independent_but_pooled_missing_scope_stays_blocked(tmp_path):
    path, inv, policy, _, inputs = source(tmp_path)
    current = {
        "season": "2026-27",
        "phase": "Regular Season",
        "first": "2026-10-22",
        "last": "2026-10-22",
    }
    combined = select(inv, policy, [request(), current], AS_OF)
    assert combined["payload"]["status"] == "refused"
    hist = reconciliation(combined, inputs, authority(inputs))
    assert hist["payload"]["membership_complete"]
    historical = coverage([hist], metric_catalog(), [query()])["payload"]["metrics"][0]
    assert historical["league_rank_eligible"]
    assert historical["level"] == "cohort_complete"
    absent = evaluation_snapshot(path, inv, current)
    current_rec = reconciliation(combined, absent, expected_inventory(current))
    mixed = coverage([hist, current_rec], metric_catalog(), [query()])["payload"]
    assert len(mixed["scopes"]) == 2
    assert not mixed["metrics"][0]["league_rank_eligible"]
    assert "scope_membership_incomplete" in mixed["metrics"][0]["reasons"]


def test_unknown_70_percent_basis_is_not_reported_as_one_game_qualification(tmp_path):
    _, _, _, chosen, inputs = source(tmp_path)
    _, metrics, _ = evaluate(chosen, inputs, expected_inventory(request()))
    metric = metrics["pts"]
    assert metric["qualified_population"] == 0
    assert all(not p["qualified"] for p in metric["players"])
    assert "qualification_game_basis_not_established" in metric["reasons"]
    assert next(p for p in metric["players"] if p["player_id"] == 10)["value"] == 4
    _, metrics, _ = evaluate(
        chosen, inputs, expected_inventory(request()), query(min_games=1)
    )
    assert metrics["pts"]["qualified_population"] == 3
    assert not metrics["pts"][
        "league_rank_eligible"
    ]  # override never substitutes coverage
