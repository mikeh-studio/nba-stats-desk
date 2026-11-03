"""Synthetic authority fixtures test capability gates; no live certification."""

import copy
import json
from datetime import date, timedelta

import pytest
from app.data.unified.evaluation import (
    coverage,
    expected_inventory,
    metric_catalog,
    reconciliation,
)
from app.data.unified.identity import ContractError, digest
from app.data.unified.ledger import LedgerStore, record
from app.data.unified.selection import policy_record, select
from app.data.unified.snapshot import evaluation_snapshot, snapshot
from app.data.unified.store import ObservationStore
from scripts import evaluate_unified_coverage as cli
from tests.support.historical import complete, ingest, payload, receipt

AS_OF = "2026-10-30T06:00:00+00:00"


def query(metric="pts", aggregation=None, **kwargs):
    definition = metric_catalog()["payload"]["metrics"][metric]
    result = {
        "metric": metric,
        "aggregation": aggregation
        or (
            "total"
            if metric == "gp"
            else "ratio"
            if definition["denominator"]
            else "average"
        ),
        "min_games": None,
        "min_attempts": None,
        "min_minutes": None,
    }
    return {**result, **kwargs}


def request(last="2025-10-23"):
    return {
        "season": "2025-26",
        "phase": "Regular Season",
        "first": "2025-10-22",
        "last": last,
    }


def source(tmp_path, complete_members=True, missing_ast=False, last="2025-10-23"):
    path = tmp_path / "source.sqlite"
    store = ObservationStore(path)
    for number, day in ((1, "2025-10-22"), (2, "2025-10-23")):
        value = payload("002250000" + str(number), day)
        value["appearances"][2]["ast"] = None if missing_ast and number == 1 else 0
        ingest(store, complete(value) if complete_members else value)
    store.close()
    inv, policy = snapshot(path), policy_record()
    chosen = select(inv, policy, [request(last)], AS_OF)
    inputs = evaluation_snapshot(path, inv, request(last))
    return path, inv, policy, chosen, inputs


def authority(inputs, mutate=None):
    requested = inputs["payload"]["scope"]
    games = [
        {
            "game_uid": o["semantic"]["game_uid"],
            "scope": o["semantic"]["scope"],
            **{
                side + "_score": o["semantic"]["result"][side + "_score"]
                for side in ("home", "away")
            },
        }
        for o in inputs["payload"]["observations"]
    ]
    first, last = (
        date.fromisoformat(requested["first"]),
        date.fromisoformat(requested["last"]),
    )
    days = [
        {
            "date": str(first + timedelta(days=n)),
            "state": "finalized",
            "game_uids": sorted(
                g["game_uid"]
                for g in games
                if g["scope"]["date"] == str(first + timedelta(days=n))
            ),
        }
        for n in range((last - first).days + 1)
    ]
    p = {
        "version": "expected_scope_inventory/1",
        "scope": requested,
        "authority": None,
        "days": days,
        "games": games,
    }
    if mutate:
        mutate(p)
    proof = b"Synthetic explicitly reviewed schedule, not real NBA authority"
    p["authority"] = {
        "method": "reviewed_scope_inventory_v1",
        "receipt": receipt(proof, "2026-10-29T00:00:00+00:00"),
        "body": proof.decode(),
        "attested_inventory_sha256": digest(
            {k: p[k] for k in ("scope", "days", "games")}
        ),
    }
    return expected_inventory(requested, p)


def evaluate(chosen, inputs, expected, *requests):
    rec = reconciliation(chosen, inputs, expected)
    result = coverage([rec], metric_catalog(), list(requests) or [query()])
    return (
        rec,
        {m["request"]["metric"]: m for m in result["payload"]["metrics"]},
        result,
    )


def test_complete_historical_scope_is_metric_qualified_without_runtime_activation(
    tmp_path,
):
    _, _, _, chosen, inputs = source(tmp_path)
    rec, metrics, result = evaluate(
        chosen, inputs, authority(inputs), query(), query("dreb"), query("gp")
    )
    assert (
        rec["payload"]["membership_complete"]
        and rec["payload"]["validated_cutoff"] == "2025-10-23"
    )
    assert (
        metrics["pts"]["level"] == "cohort_complete"
        and metrics["pts"]["league_rank_eligible"]
    )
    assert (
        metrics["pts"]["percentile_eligible"]
        and metrics["pts"]["qualified_population"] == 3
    )
    assert (
        next(p for p in metrics["pts"]["players"] if p["player_id"] == 12)["value"] == 0
    )
    assert all(p["value"] == 2 for p in metrics["gp"]["players"])
    assert (
        not result["payload"]["runtime_eligible"]
        and not result["payload"]["pdf_corroborated"]
    )


@pytest.mark.parametrize("missing", ["authority", "participants"])
def test_observed_rows_and_scores_cannot_create_completeness(tmp_path, missing):
    _, _, _, chosen, inputs = source(
        tmp_path, complete_members=missing != "participants"
    )
    expected = (
        expected_inventory(request()) if missing == "authority" else authority(inputs)
    )
    rec, metrics, _ = evaluate(chosen, inputs, expected)
    assert not rec["payload"]["membership_complete"]
    assert (
        metrics["pts"]["level"] == "observed_sample"
        and not metrics["pts"]["league_rank_eligible"]
    )
    assert (
        next(p for p in metrics["pts"]["players"] if p["player_id"] == 10)["value"] == 4
    )


def test_missing_game_and_no_game_dates_control_contiguous_cutoff(tmp_path):
    _, _, _, chosen, inputs = source(tmp_path, last="2025-10-24")
    rec, _, _ = evaluate(chosen, inputs, authority(inputs))
    assert rec["payload"]["validated_cutoff"] == "2025-10-24"  # explicit no-game day

    def missing(p):
        game = copy.deepcopy(p["games"][0])
        game["game_uid"] = "g_" + "f" * 32
        game["scope"]["date"] = "2025-10-24"
        p["games"].append(game)
        p["days"][2]["game_uids"].append(game["game_uid"])

    rec, metrics, _ = evaluate(chosen, inputs, authority(inputs, missing))
    assert (
        rec["payload"]["missing_games"]
        and rec["payload"]["validated_cutoff"] == "2025-10-23"
    )
    assert not metrics["pts"]["league_rank_eligible"]
    with pytest.raises(ContractError, match="every date"):
        authority(inputs, lambda p: p["days"].pop())


def test_unsettled_date_and_future_authority_do_not_certify_cutoff(tmp_path):
    _, _, _, chosen, inputs = source(tmp_path)
    expected = authority(inputs, lambda p: p["days"][1].update(state="unsettled"))
    rec, metrics, _ = evaluate(chosen, inputs, expected)
    assert (
        rec["payload"]["validated_cutoff"] == "2025-10-22"
        and not metrics["pts"]["league_rank_eligible"]
    )
    expected = authority(inputs)
    p = copy.deepcopy(expected["payload"])
    p["authority"]["receipt"]["captured_at"] = "2026-10-31T00:00:00+00:00"
    rec, _, _ = evaluate(chosen, inputs, expected_inventory(request(), p))
    assert rec["payload"]["validated_cutoff"] is None


def test_component_dispute_holds_only_dependent_metrics(tmp_path):
    path, inv, _, chosen, inputs = source(tmp_path)
    dispute = {
        "observation_id": inputs["payload"]["observations"][0]["observation_id"],
        "kind": "component",
        "components": ["ast"],
        "detail": "Synthetic source assist disagreement",
        "evidence": receipt(captured_at="2026-10-29T00:00:00+00:00"),
    }
    changed = evaluation_snapshot(path, inv, request(), [dispute])
    rec, metrics, _ = evaluate(
        chosen,
        changed,
        authority(inputs),
        query(),
        query("ast"),
        query("points_created"),
    )
    assert rec["payload"]["membership_complete"]
    assert metrics["pts"]["league_rank_eligible"]
    assert (
        not metrics["ast"]["league_rank_eligible"]
        and not metrics["points_created"]["league_rank_eligible"]
    )


def test_result_and_identity_disputes_never_select_other_provider_values(tmp_path):
    path, inv, _, chosen, inputs = source(tmp_path)
    dispute = {
        "observation_id": inputs["payload"]["observations"][0]["observation_id"],
        "kind": "result",
        "components": [],
        "detail": "Alternative result differs",
        "evidence": receipt(captured_at="2026-10-29T00:00:00+00:00"),
    }
    rec, metrics, _ = evaluate(
        chosen,
        evaluation_snapshot(path, inv, request(), [dispute]),
        authority(inputs),
        query("gp"),
        query(),
    )
    assert (
        not rec["payload"]["membership_complete"]
        and rec["payload"]["validated_cutoff"] is None
    )
    assert not metrics["gp"]["league_rank_eligible"]
    assert (
        next(p for p in metrics["pts"]["players"] if p["player_id"] == 10)["value"] == 4
    )


def test_missing_ast_and_legacy_minute_origins_do_not_block_points(tmp_path):
    _, _, _, chosen, inputs = source(tmp_path, missing_ast=True)
    _, metrics, _ = evaluate(
        chosen,
        inputs,
        authority(inputs),
        query(),
        query("ast"),
        query("pts_per36", min_minutes=1),
    )
    assert metrics["pts"]["league_rank_eligible"]
    assert (
        not metrics["ast"]["league_rank_eligible"]
        and not metrics["pts_per36"]["league_rank_eligible"]
    )
    minute_player = next(
        p for p in metrics["pts_per36"]["players"] if p["player_id"] == 12
    )
    assert minute_player["games"] == 2 and minute_player["valid_appearances"] == 0
    assert minute_player["value"] is None


def test_attempt_thresholds_zero_denominators_and_overrides_are_explicit(tmp_path):
    _, _, _, chosen, inputs = source(tmp_path)
    _, metrics, _ = evaluate(chosen, inputs, authority(inputs), query("fg_pct"))
    assert "attempt_threshold_required" in metrics["fg_pct"]["reasons"]
    _, metrics, _ = evaluate(
        chosen, inputs, authority(inputs), query("fg_pct", min_attempts=1)
    )
    zero = next(p for p in metrics["fg_pct"]["players"] if p["player_id"] == 12)
    assert zero["value"] is None and not zero["qualified"]
    _, metrics, _ = evaluate(
        chosen, inputs, expected_inventory(request()), query(min_games=1)
    )
    assert not metrics["pts"]["league_rank_eligible"]
    with pytest.raises(ValueError):
        evaluate(chosen, inputs, authority(inputs), query(min_games=True))


def test_pinned_input_and_zero_point_membership_bindings_cannot_be_forged(tmp_path):
    _, inv, _, _, inputs = source(tmp_path)
    changed = copy.deepcopy(inputs["payload"])
    changed["observations"][0]["semantic"]["appearances"].pop()
    forged = record("evaluation_inputs", changed, inputs["depends_on"])
    store = LedgerStore()
    store.append(inv)
    with pytest.raises(ContractError, match="integrity"):
        store.append(forged)
    store.close()


def test_ledger_recomputes_evaluation_and_rejects_forged_coverage(tmp_path):
    _, inv, policy, chosen, inputs = source(tmp_path)
    expected = authority(inputs)
    rec, _, result = evaluate(chosen, inputs, expected)
    store = LedgerStore()
    values = [policy, inv, chosen, inputs, expected, metric_catalog(), rec, result]
    for value in values:
        assert store.append(value) == "recorded"
    for value in values:
        assert store.append(value) == "noop"
    assert store.read(result["record_id"]) == result
    changed = copy.deepcopy(result["payload"])
    changed["metrics"][0]["players"][0]["value"] = 999
    with pytest.raises(ContractError, match="recomputation"):
        store.append(record("coverage", changed, result["depends_on"]))
    store.close()


def test_cli_preserves_originals_and_keeps_missing_real_authority_observed(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    path, _, _, chosen, _ = source(tmp_path)
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps(chosen))
    before = path.read_bytes()
    requests = [query(), query("dreb")]
    first = cli.run(
        path,
        selection,
        tmp_path / "reports/new.sqlite",
        tmp_path / "reports/first",
        requests,
    )
    second = cli.run(
        path,
        selection,
        tmp_path / "reports/new.sqlite",
        tmp_path / "reports/replay",
        requests,
    )
    assert first["recorded"] == 8 and second["noops"] == 8
    assert first["coverage_id"] == second["coverage_id"]
    assert all(
        m["level"] == "observed_sample" and not m["league_rank_eligible"]
        for m in first["metrics"]
    )
    assert path.read_bytes() == before


def test_rehashed_zero_point_omission_still_fails_participant_membership(tmp_path):
    _, inv, _, _, inputs = source(tmp_path)
    changed = copy.deepcopy(inputs["payload"])
    obs = changed["observations"][0]
    old = obs["observation_id"]
    obs["semantic"]["appearances"] = [
        p for p in obs["semantic"]["appearances"] if p["player_id"] != 12
    ]
    obs["fact_revision"] = digest(obs["semantic"])
    obs.pop("observation_id")
    obs["observation_id"] = digest(obs)
    p = copy.deepcopy(inv["payload"])
    ref = next(r for r in p["observations"] if r["observation_id"] == old)
    ref.update(
        observation_id=obs["observation_id"],
        fact_revision=obs["fact_revision"],
        appearances=2,
    )
    pinned = record("inventory", p)
    changed["inventory_id"] = pinned["record_id"]
    changed["observations"].sort(key=lambda o: o["observation_id"])
    forged = record("evaluation_inputs", changed, [pinned["record_id"]])
    store = LedgerStore()
    store.append(pinned)
    with pytest.raises(ContractError, match="Zero-point/played"):
        store.append(forged)
    store.close()


def test_capture_clocks_cannot_be_removed_from_inventory_bindings(tmp_path):
    _, inv, _, _, inputs = source(tmp_path)
    p = copy.deepcopy(inv["payload"])
    p["observations"][0]["capture_times"] = []
    pinned = record("inventory", p)
    changed = copy.deepcopy(inputs["payload"])
    changed["inventory_id"] = pinned["record_id"]
    store = LedgerStore()
    store.append(pinned)
    with pytest.raises(ContractError, match="clock binding"):
        store.append(record("evaluation_inputs", changed, [pinned["record_id"]]))
    store.close()


def test_mixed_season_ratios_pool_components_and_current_freshness_stays_blocked(
    tmp_path,
):
    from test_unified_espn import retained, seed

    path = tmp_path / "mixed.sqlite"
    store = ObservationStore(path)
    seed(store)
    for n, day in ((1, "2025-10-22"), (2, "2025-10-23")):
        value = payload("002250000" + str(n), day)
        value["appearances"][1]["fga"] = 4
        value["appearances"][2]["ast"] = 0
        ingest(store, complete(value))
    ingest(store, retained())
    store.close()
    current = {
        "season": "2026-27",
        "phase": "Regular Season",
        "first": "2026-10-22",
        "last": "2026-10-22",
    }
    inv = snapshot(path)
    chosen = select(inv, policy_record(), [request(), current], AS_OF)
    recs = []
    for s in (request(), current):
        inputs = evaluation_snapshot(path, inv, s)
        recs.append(reconciliation(chosen, inputs, authority(inputs)))
    result = coverage(recs, metric_catalog(), [query("fg_pct", min_attempts=1)])
    metric = result["payload"]["metrics"][0]
    player = next(p for p in metric["players"] if p["player_id"] == 11)
    assert player["games"] == player["valid_appearances"] == 3
    assert player["value"] == pytest.approx(3 / 9)
    assert "current_freshness_not_established" in metric["reasons"]
    assert not metric["league_rank_eligible"] and not metric["percentile_eligible"]
