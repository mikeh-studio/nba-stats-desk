"""Offline, revision-bound reconciliation and metric coverage; no serving writes."""

from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any

from app.agent.formulas import evaluate_formula

from .historical import COUNTS, validated_stats
from .identity import digest, evidence, require, scope
from .ledger import CONTRACTS, record, validate
from .selection import request_scope, timestamp
from .store import parse_json

COMPONENTS = set(COUNTS) | {"min"}


def metric_catalog():
    return record(
        "metric_catalog",
        parse_json((CONTRACTS / "evaluation-metrics.json").read_bytes()),
    )


def expected_inventory(requested_scope, payload=None):
    value = payload or {
        "version": "expected_scope_inventory/1",
        "scope": requested_scope,
        "authority": None,
        "days": [],
        "games": [],
    }
    require(
        value["scope"] == request_scope(requested_scope),
        "Expected inventory request scope differs",
    )
    result = record("expected_inventory", value)
    validate_expected(result)
    return result


def validate_expected(value):
    validate(value)
    require(value["kind"] == "expected_inventory", "Expected inventory record required")
    p = value["payload"]
    requested = request_scope(p["scope"])
    if p["authority"] is None:
        require(
            not p["days"] and not p["games"],
            "Unverified inventory cannot declare expected membership",
        )
        return
    proof = p["authority"]
    receipt = evidence(proof["receipt"])
    require(
        receipt["kind"] in ("reviewed_identity", "synthetic_fixture")
        and receipt["captured_at"] is not None,
        "Expected inventory requires an explicit reviewed authority receipt",
    )
    require(
        proof["body"]
        and hashlib.sha256(proof["body"].encode()).hexdigest() == receipt["sha256"],
        "Authority evidence hash differs",
    )
    require(
        proof["attested_inventory_sha256"]
        == digest({k: p[k] for k in ("scope", "days", "games")}),
        "Authority inventory binding differs",
    )
    first, last = (
        date.fromisoformat(requested["first"]),
        date.fromisoformat(requested["last"]),
    )
    days = [str(first + timedelta(days=n)) for n in range((last - first).days + 1)]
    require(
        [d["date"] for d in p["days"]] == days,
        "Expected inventory must explicitly cover every date, including no-game dates",
    )
    games = {g["game_uid"]: g for g in p["games"]}
    require(len(games) == len(p["games"]), "Duplicate expected game")
    seen = set()
    for day in p["days"]:
        require(
            day["game_uids"] == sorted(set(day["game_uids"])),
            "Duplicate/unsorted expected day membership",
        )
        for uid in day["game_uids"]:
            require(
                uid in games and uid not in seen, "Expected day/game membership differs"
            )
            seen.add(uid)
            g = games[uid]
            s = scope(g["scope"])
            require(
                s["date"] == day["date"]
                and s["season"] == requested["season"]
                and s["phase"] == requested["phase"],
                "Expected game scope differs",
            )
            require(
                g["home_score"] != g["away_score"], "Expected final result cannot tie"
            )
    require(seen == set(games), "Expected games omitted from day membership")


def validate_inputs(value, inventory):
    validate(value)
    require(
        value["kind"] == "evaluation_inputs"
        and value["payload"]["inventory_id"] == inventory["record_id"],
        "Evaluation inventory binding differs",
    )
    p = value["payload"]
    requested = request_scope(p["scope"])
    refs = {
        r["observation_id"]: r
        for r in inventory["payload"]["observations"]
        if r["scope"]["season"] == requested["season"]
        and r["scope"]["phase"] == requested["phase"]
        and requested["first"] <= r["scope"]["date"] <= requested["last"]
    }
    require(
        [o["observation_id"] for o in p["observations"]] == sorted(refs),
        "Evaluation observations differ from pinned scope inventory",
    )
    for obs in p["observations"]:
        semantic = obs["semantic"]
        require(
            digest(semantic) == obs["fact_revision"]
            and digest({k: v for k, v in obs.items() if k != "observation_id"})
            == obs["observation_id"],
            "Evaluation observation integrity failed",
        )
        ref = refs[obs["observation_id"]]
        clocks = [obs["provenance"]["captured_at"]]
        if "source_chain" in obs:
            clocks.append(obs["source_chain"]["source"]["captured_at"])
        if "date_review" in obs:
            clocks.append(
                obs["date_review"]["review"]["authority"]["receipt"]["captured_at"]
            )
        if obs["membership"]["participant_manifest"]:
            clocks.append(
                obs["membership"]["participant_manifest"]["evidence"]["captured_at"]
            )
        require(
            ref["capture_times"] == sorted({t for t in clocks if t is not None}),
            "Capture clock binding differs from pinned inventory",
        )
        require(
            all(semantic[k] == ref[k] for k in ("game_uid", "provider", "scope"))
            and obs["fact_revision"] == ref["fact_revision"]
            and obs["source_game_id"] == ref["source_game_id"]
            and len(semantic["appearances"]) == ref["appearances"]
            and semantic["membership_level"] == ref["membership"],
            "Evaluation observation metadata binding differs",
        )
        require(
            semantic["semantics_version"] == "unified_semantics/1.0.0"
            and semantic["source_definition"]
            in ("nba_game_logs_reported_v1", "espn_summary_reported_v1"),
            "Unregistered statistical definition",
        )
        seen = set()
        scores: Counter[str] = Counter()
        for player in semantic["appearances"]:
            pid, team = player["player_id"], player["team_at_game"]
            require(
                pid not in seen
                and team in (semantic["scope"]["home"], semantic["scope"]["away"]),
                "Invalid canonical appearance membership",
            )
            seen.add(pid)
            side = "home" if team == semantic["scope"]["home"] else "away"
            other = "away" if side == "home" else "home"
            require(
                player["opponent"] == semantic["scope"][other]
                and player["home_away"] == side.upper(),
                "Appearance game role differs",
            )
            validated_stats(player["stats"])
            scores[side] += player["stats"]["pts"]
        require(
            all(
                scores[side] == semantic["result"][side + "_score"]
                for side in ("home", "away")
            ),
            "Appearance/result arithmetic differs",
        )
        require(
            {p["team_at_game"] for p in semantic["appearances"]}
            == {semantic["scope"]["home"], semantic["scope"]["away"]},
            "Both played teams are required",
        )
        member = obs["membership"]
        require(
            member["level"] == semantic["membership_level"],
            "Membership level binding differs",
        )
        if member["level"] == "provider_declared_complete":
            manifest = member["participant_manifest"]
            require(
                manifest is not None
                and manifest["evidence"]["captured_at"] is not None,
                "Complete participant evidence missing",
            )
            require(
                manifest["evidence"]["kind"]
                in ("provider_capture", "synthetic_fixture"),
                "Identity-only evidence cannot establish participant completeness",
            )
            participants = manifest["participants"]
            require(
                len({r["player_id"] for r in participants}) == len(participants),
                "Duplicate participant manifest identity",
            )
            require(
                all(
                    r["team"] in (semantic["scope"]["home"], semantic["scope"]["away"])
                    for r in participants
                ),
                "Participant team differs",
            )
            require(
                {(r["player_id"], r["team"]) for r in participants if r["played"]}
                == {
                    (r["player_id"], r["team_at_game"]) for r in semantic["appearances"]
                },
                "Zero-point/played participant membership differs",
            )
    for dispute in p["disputes"]:
        require(
            dispute["observation_id"] in refs,
            "Dispute references an unpinned observation",
        )
        receipt = evidence(dispute["evidence"])
        require(
            receipt["kind"] in ("provider_capture", "synthetic_fixture")
            and receipt["captured_at"] is not None,
            "Statistical dispute needs capture evidence",
        )
        require(
            dispute["components"] == sorted(set(dispute["components"])),
            "Duplicate/unsorted dispute components",
        )
        require(
            bool(dispute["components"]) == (dispute["kind"] == "component"),
            "Dispute component scope differs",
        )
    from datetime import datetime, timezone

    from .freshness import revalidation_times

    revalidation_times(
        p["observations"],
        p.get("revalidations", []),
        datetime.max.replace(tzinfo=timezone.utc),
    )


def reconciliation(selection, inputs, authority):
    for value in (selection, inputs, authority):
        validate(value)
    validate_expected(authority)
    require(
        selection["payload"]["inventory_id"] == inputs["payload"]["inventory_id"],
        "Reconciliation selection/input inventory binding differs",
    )
    requested = inputs["payload"]["scope"]
    require(
        authority["payload"]["scope"] == requested,
        "Reconciliation authority scope differs",
    )
    matches = [s for s in selection["payload"]["scopes"] if s["scope"] == requested]
    require(len(matches) == 1, "Reconciliation scope outside selection")
    selected_scope = matches[0]
    by_id = {o["observation_id"]: o for o in inputs["payload"]["observations"]}
    require(
        set(selected_scope["selected"]) <= set(by_id),
        "Selected observations absent from evaluation input",
    )
    selected = [by_id[oid] for oid in selected_scope["selected"]]
    games = {o["semantic"]["game_uid"]: o for o in selected}
    require(len(games) == len(selected), "Duplicate selected canonical game")
    reasons = set()
    if selected_scope["status"] != "selected_observed":
        reasons.add("selection_not_usable")
    expected = authority["payload"]
    known = expected["authority"] is not None
    if not known:
        reasons.add("expected_universe_not_established")
    elif timestamp(expected["authority"]["receipt"]["captured_at"]) > timestamp(
        selection["payload"]["selection_as_of"]
    ):
        reasons.add("authority_after_selection_time")
    expected_games = {g["game_uid"]: g for g in expected["games"]}
    missing = sorted(set(expected_games) - set(games)) if known else []
    unexpected = sorted(set(games) - set(expected_games)) if known else []
    incomplete = sorted(
        uid
        for uid, o in games.items()
        if o["semantic"]["membership_level"] != "provider_declared_complete"
    )
    mismatches = []
    for uid in set(expected_games) & set(games):
        fact = games[uid]["semantic"]
        if fact["scope"] != expected_games[uid]["scope"] or any(
            fact["result"][side + "_score"] != expected_games[uid][side + "_score"]
            for side in ("home", "away")
        ):
            mismatches.append(uid)
    for rows, code in (
        (missing, "missing_expected_games"),
        (unexpected, "unexpected_observed_games"),
        (incomplete, "participant_lineage_incomplete"),
        (mismatches, "result_or_scope_discrepancy"),
    ):
        if rows:
            reasons.add(code)
    holds = set()
    for dispute in inputs["payload"]["disputes"]:
        if dispute["observation_id"] not in selected_scope["selected"]:
            continue
        require(
            timestamp(dispute["evidence"]["captured_at"])
            <= timestamp(selection["payload"]["selection_as_of"]),
            "Dispute evidence postdates selection",
        )
        if dispute["kind"] in ("identity", "membership", "result"):
            reasons.add("identity_membership_or_result_dispute")
            holds.update(COMPONENTS)
        else:
            holds.update(
                dispute["components"] if dispute["kind"] == "component" else ["min"]
            )
    if holds:
        reasons.add("component_discrepancy")
    closed = known and all(d["state"] == "finalized" for d in expected["days"])
    if known and not closed:
        reasons.add("unsettled_inventory_dates")
    complete = closed and not (reasons - {"component_discrepancy"})
    cutoff = None
    if known and not (
        {
            "authority_after_selection_time",
            "selection_not_usable",
            "identity_membership_or_result_dispute",
        }
        & reasons
    ):
        for day in expected["days"]:
            if (
                day["state"] != "finalized"
                or any(
                    games[uid]["semantic"]["scope"]["date"] == day["date"]
                    for uid in unexpected
                )
                or any(
                    uid not in games or uid in incomplete or uid in mismatches
                    for uid in day["game_uids"]
                )
            ):
                break
            cutoff = day["date"]
    groups: dict[tuple[int, tuple[str, ...], str], dict[str, Any]] = {}
    for uid, obs in sorted(games.items()):
        for player in obs["semantic"]["appearances"]:
            values = {k: v for k, v in player["stats"].items() if v is not None}
            minute = player["minutes"]
            if (
                minute["value"] is not None
                and minute["precision"] == "whole_minute_reported"
                and minute["zero_origin"] != "unverified_legacy_zero"
            ):
                values["min"] = minute["value"]
            key = (player["player_id"], tuple(sorted(values)), minute["precision"])
            group = groups.setdefault(
                key,
                {
                    "player_id": player["player_id"],
                    "components": sorted(values),
                    "game_uids": [],
                    "sums": {k: 0 for k in values},
                    "minute_precision": minute["precision"],
                },
            )
            group["game_uids"].append(uid)
            for component, value in values.items():
                group["sums"][component] += value
    team_games = Counter(
        t
        for g in expected_games.values()
        for t in (g["scope"]["home"], g["scope"]["away"])
    )
    from .freshness import assess, correction_reviews

    freshness, freshness_reasons = assess(
        requested,
        authority,
        selected,
        inputs["payload"].get("revalidations", []),
        timestamp(selection["payload"]["selection_as_of"]),
    )
    reviews = correction_reviews(
        selected,
        inputs["payload"].get("revalidations", []),
        inputs["payload"]["disputes"],
        mismatches,
        timestamp(selection["payload"]["selection_as_of"]),
    )
    status = (
        "unavailable"
        if not selected
        else "discrepant"
        if mismatches or holds
        else "consistent"
        if complete
        else "incomplete"
    )
    payload = {
        "version": "scope_reconciliation/1",
        "selection_id": selection["record_id"],
        "inputs_id": inputs["record_id"],
        "authority_id": authority["record_id"],
        "scope": requested,
        "provider": selected_scope["provider"],
        "status": status,
        "reasons": sorted(reasons),
        "selected_observations": sorted(selected_scope["selected"]),
        "expected_games": len(expected_games) if known else None,
        "missing_games": missing,
        "unexpected_games": unexpected,
        "participant_incomplete_games": incomplete,
        "result_mismatches": sorted(mismatches),
        "validated_cutoff": cutoff,
        "max_team_games": max(team_games.values(), default=0) if known else None,
        "groups": [groups[k] for k in sorted(groups)],
        "component_holds": sorted(holds),
        "membership_complete": bool(complete),
        "current_freshness": freshness,
        "freshness_reasons": freshness_reasons,
        "correction_reviews": reviews,
        "runtime_eligible": False,
    }
    return record(
        "reconciliation",
        payload,
        (selection["record_id"], inputs["record_id"], authority["record_id"]),
    )


def coverage(reconciliations, catalog, requests):
    validate(catalog)
    require(catalog["kind"] == "metric_catalog", "Metric definition record required")
    require(
        1 <= len(reconciliations) <= 2 and 1 <= len(requests) <= 24,
        "Coverage scope/metric bound exceeded",
    )
    for r in reconciliations:
        validate(r)
        require(
            r["kind"] == "reconciliation", "Coverage requires reconciliation records"
        )
    scopes = [r["payload"]["scope"] for r in reconciliations]
    require(
        len({s["season"] for s in scopes}) == len(scopes), "Duplicate coverage season"
    )
    reconciliations = sorted(
        reconciliations, key=lambda r: r["payload"]["scope"]["season"]
    )
    require(
        len({r["payload"]["selection_id"] for r in reconciliations}) == 1,
        "Mixed selection versions cannot be pooled",
    )
    groups = [g for r in reconciliations for g in r["payload"]["groups"]]
    seen = set()
    for group in groups:
        for uid in group["game_uids"]:
            require(
                (uid, group["player_id"]) not in seen,
                "Duplicate pooled canonical appearance",
            )
            seen.add((uid, group["player_id"]))
    by_player = defaultdict(list)
    for group in groups:
        by_player[group["player_id"]].append(group)
    defs = catalog["payload"]["metrics"]
    require(
        len({q["metric"] for q in requests}) == len(requests),
        "Duplicate metric request",
    )
    results = []
    for query in sorted(requests, key=lambda q: q["metric"]):
        require(
            set(query)
            == {"metric", "aggregation", "min_games", "min_attempts", "min_minutes"}
            and query["metric"] in defs,
            "Unsupported metric request",
        )
        metric = defs[query["metric"]]
        require(
            query["aggregation"] in metric["aggregations"],
            "Unsupported metric aggregation",
        )
        required = set(metric["components"])
        for field in ("min_games", "min_attempts", "min_minutes"):
            value = query[field]
            require(
                value is None
                or (
                    type(value) in (int, float)
                    and math.isfinite(value)
                    and value > 0
                    and (field != "min_games" or type(value) is int)
                ),
                "Qualification threshold must be explicitly positive",
            )
        max_games = [r["payload"]["max_team_games"] for r in reconciliations]
        qualification_known = (
            query["min_games"] is not None
            or query["aggregation"] == "total"
            or all(g is not None for g in max_games)
        )
        minimum = query["min_games"] or (
            1
            if query["aggregation"] == "total"
            else max(1, math.ceil(0.70 * sum(g or 0 for g in max_games)))
        )
        reasons = set()
        if not qualification_known:
            reasons.add("qualification_game_basis_not_established")
        if not all(r["payload"]["membership_complete"] for r in reconciliations):
            reasons.add("scope_membership_incomplete")
        if required & {
            c for r in reconciliations for c in r["payload"]["component_holds"]
        }:
            reasons.add("dependent_component_discrepancy")
        if (
            metric["denominator"]
            and metric["unit"] != "per36"
            and query["min_attempts"] is None
        ):
            reasons.add("attempt_threshold_required")
        if metric["unit"] == "per36" and query["min_minutes"] is None:
            reasons.add("minute_threshold_required")
        players = []
        potential_missing = False
        for pid, buckets in sorted(by_player.items()):
            games = sum(len(g["game_uids"]) for g in buckets)
            valid = [g for g in buckets if required <= set(g["components"])]
            n = sum(len(g["game_uids"]) for g in valid)
            sums = {c: sum(g["sums"][c] for g in valid) for c in required}
            numerator = evaluate_formula(metric["numerator"], sums) if n else None
            if query["metric"] == "gp":
                numerator = float(n)
            denominator = (
                evaluate_formula(metric["denominator"], sums)
                if n and metric["denominator"]
                else None
            )
            value = (
                None
                if numerator is None
                else numerator / denominator
                if metric["denominator"] and denominator and denominator > 0
                else None
                if metric["denominator"]
                else numerator / n
                if query["aggregation"] == "average"
                else numerator
            )
            if value is not None:
                require(math.isfinite(value), "Nonfinite metric result")
            why = set()
            if not qualification_known:
                why.add("qualification_game_basis_not_established")
            if n < games:
                why.add("missing_metric_components")
                if games >= minimum:
                    potential_missing = True
            if n < minimum:
                why.add("minimum_games_not_met")
            if value is None:
                why.add("undefined_metric_value")
            if (
                metric["denominator"]
                and metric["unit"] != "per36"
                and (
                    query["min_attempts"] is None
                    or sums.get(metric["attempt_field"], 0) < query["min_attempts"]
                )
            ):
                why.add("attempt_qualification_not_met")
            if metric["unit"] == "per36" and (
                query["min_minutes"] is None
                or sums.get("min", 0) < query["min_minutes"]
            ):
                why.add("minute_qualification_not_met")
            player_complete = (
                all(r["payload"]["membership_complete"] for r in reconciliations)
                and n == games
                and "dependent_component_discrepancy" not in reasons
            )
            level = (
                "unavailable"
                if n == 0
                else "player_complete"
                if player_complete
                else "observed_sample"
            )
            players.append(
                {
                    "player_id": pid,
                    "games": games,
                    "valid_appearances": n,
                    "value": value,
                    "level": level,
                    "qualified": not why,
                    "reasons": sorted(why),
                }
            )
        if potential_missing:
            reasons.add("qualifying_population_components_incomplete")
        population = sum(p["qualified"] for p in players)
        cohort = not reasons
        if not population:
            reasons.add("no_qualified_population")
        fresh = all(
            r["payload"]["current_freshness"] in ("not_required", "fresh")
            for r in reconciliations
        )
        if not fresh:
            reasons.add(
                "current_freshness_stale"
                if any(
                    r["payload"]["current_freshness"] == "stale"
                    for r in reconciliations
                )
                else "current_freshness_not_established"
            )
        level = (
            "unavailable"
            if not any(p["valid_appearances"] for p in players)
            else "cohort_complete"
            if cohort
            else "player_complete"
            if any(p["level"] == "player_complete" for p in players)
            else "observed_sample"
        )
        rank = cohort and population > 0 and fresh
        results.append(
            {
                "request": query,
                "level": level,
                "qualified_population": population,
                "league_rank_eligible": rank,
                "percentile_eligible": rank and population > 1,
                "reasons": sorted(reasons),
                "players": players,
            }
        )
    payload = {
        "version": "metric_coverage/1",
        "reconciliation_ids": sorted(r["record_id"] for r in reconciliations),
        "metric_catalog_id": catalog["record_id"],
        "selection_id": reconciliations[0]["payload"]["selection_id"],
        "requests": sorted(requests, key=lambda q: q["metric"]),
        "metrics": results,
        "scopes": [r["payload"]["scope"] for r in reconciliations],
        "publication_status": "offline_only",
        "runtime_eligible": False,
        "pdf_corroborated": False,
    }
    return record(
        "coverage", payload, (*payload["reconciliation_ids"], catalog["record_id"])
    )
