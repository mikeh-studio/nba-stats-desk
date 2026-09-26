#!/usr/bin/env python3
"""Build a versioned multi-stat catalog from frozen study input descriptors.

Each descriptor names a spec, stats snapshot, schedule, injuries, memberships,
and optional context SQLite database and reviewed causal rows/spec. All paths
are operator supplied, never accepted from a public endpoint.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.semantic_source import load_snapshot  # noqa: E402
from app.agent.semantics import aggregate, load_contract  # noqa: E402
from app.agent.teammate_readiness import build_panel, schedule_games  # noqa: E402
from app.research import CORE_METRICS, SHOOTING_METRICS  # noqa: E402
from app.research_snapshots import append_snapshot, digest  # noqa: E402
from app.research_studies import PAIRS  # noqa: E402
from scripts.causal_estimation import estimate  # noqa: E402
from scripts.teammate_association import analyze  # noqa: E402


def build_study(
    pair,
    spec,
    stats,
    reports,
    games,
    memberships,
    context=(),
    causal_rows=(),
    causal_spec=None,
):
    if (spec["player_id"], spec["teammate_id"]) != (
        pair["player_id"],
        pair["teammate_id"],
    ):
        raise ValueError("Study specification has different player roles")
    panel = build_panel(stats, reports, games, memberships, spec, context)
    input_hashes = {
        "spec": digest(spec),
        "stats": digest(stats),
        "reports": digest(reports),
        "games": digest(games),
        "memberships": digest(memberships),
        "context": digest(list(context)),
    }
    if causal_spec:
        expected_scope = {
            k: spec[k]
            for k in ("player_id", "teammate_id", "season", "start", "end", "team_abbr")
        }
        if (
            causal_spec.get("scope") != expected_scope
            or causal_spec.get("input_hashes") != input_hashes
        ):
            raise ValueError(
                "Causal specification must bind the exact pair, window, and frozen source hashes"
            )
        if causal_spec.get("panel_hash") != digest(list(causal_rows)):
            raise ValueError(
                "Causal specification must bind the reviewed eligible panel"
            )
        game_ids = {r["game_id"] for r in panel}
        if (
            causal_spec.get("treatment_definition") != "unavailable_minus_available"
            or causal_spec.get("estimand") != "eligible_scheduled_game_ATE"
            or not causal_spec.get("eligibility_rule")
            or set(causal_spec.get("eligible_game_ids", []))
            != {r["game_id"] for r in causal_rows}
        ):
            raise ValueError(
                "Causal panel requires a declared eligible population and unavailable-versus-available contrast"
            )
        observed = {r["game_id"]: r["outcomes"] for r in panel if r.get("outcomes")}
        for row in causal_rows:
            factual = observed.get(row["game_id"])
            if factual:
                if any(
                    row.get("outcomes", {}).get(k) != factual.get(k)
                    for k in CORE_METRICS
                ):
                    raise ValueError(
                        "Causal outcomes disagree with frozen focal statistics"
                    )
            elif row.get("outcome_complete") and (
                not row.get("nonparticipation_source_urls")
                or any(row.get("outcomes", {}).get(k) != 0 for k in CORE_METRICS)
            ):
                raise ValueError(
                    "Complete nonparticipation outcomes require explicit source verification"
                )
        if any(
            r["game_id"] not in game_ids
            or r.get("player_id") != spec["player_id"]
            or r.get("teammate_id") != spec["teammate_id"]
            or r.get("season") != spec["season"]
            for r in causal_rows
        ):
            raise ValueError(
                "Causal rows must belong to the frozen scheduled-game study"
            )
    contract = load_contract()
    groups = [
        [r["outcomes"] for r in panel if r["included"] and r["exposure"] == group]
        for group in ("participated", "reported_out_no_appearance")
    ]
    metrics = []
    for key in (*CORE_METRICS, *SHOOTING_METRICS):
        metric = contract.metric(key)
        a, b = [
            aggregate(rows, metric, "ratio" if metric.denominator else "average")
            for rows in groups
        ]
        scale = 100 if metric.unit == "ratio" else 1
        raw_difference = (
            (b["value"] - a["value"]) * scale
            if a["value"] is not None
            and b["value"] is not None
            and not a["missing_component_games"]
            and not b["missing_component_games"]
            else None
        )
        adjusted = (
            analyze(panel, reports, outcome=key)
            if key in CORE_METRICS and panel
            else None
        )
        primary = adjusted["primary"] if adjusted else None
        causal = (
            estimate(causal_rows, causal_spec, key) if key in CORE_METRICS else None
        )
        status = (
            causal["claim_level"]
            if causal and causal.get("estimate") is not None
            else "adjusted_association"
            if primary and primary["status"] == "estimated_exploratory"
            else "descriptive"
            if raw_difference is not None
            else "insufficient_evidence"
        )
        metrics.append(
            {
                "metric": key,
                "label": metric.label,
                "unit": "percentage points"
                if scale == 100
                else "score-margin points per game"
                if key == "plus_minus"
                else "minutes per game"
                if key == "min"
                else "per game",
                "status": status,
                "reason": causal.get("reason", "")
                if causal
                else "Shooting ratios are descriptive",
                "descriptive": {
                    "participated": a["display_value"],
                    "reported_out": b["display_value"],
                    "difference": raw_difference,
                    "groups": [a, b],
                    "denominator": "focal appearances",
                },
                "association": primary,
                "diagnostics": {
                    k: adjusted[k]
                    for k in (
                        "excluded",
                        "missing_counts",
                        "month_exposure_counts",
                        "covariate_balance",
                        "leave_episode_out",
                        "overlap_only",
                        "same_sample_raw_difference",
                    )
                }
                if adjusted
                else None,
                "causal": causal,
            }
        )
    return {
        **pair,
        "status": "reviewable",
        "scope": {k: spec[k] for k in ("season", "start", "end", "team_abbr")},
        "metrics": metrics,
        "panel": panel,
        "input_hashes": input_hashes,
        "builder_hash": digest(Path(__file__).read_text()),
        "causal_spec_hash": digest(causal_spec) if causal_spec else None,
        "membership_assumptions": sorted(
            {
                m["basis"]
                for m in memberships
                if m["player_id"] in (pair["player_id"], pair["teammate_id"])
            }
        ),
        "limitations": [
            "Retrospective reconstructed study; historical knowledge is not reconstructed from corrected statistics.",
            "Descriptive and adjusted results condition on focal participation and compare reported Out/no appearance with participated.",
            "Causal results require a separate reviewed eligible scheduled-game panel and identification specification.",
            "Nominal association intervals are diagnostic, not validated causal uncertainty.",
            "Unmeasured health, coaching and incomplete availability can prevent causal identification.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True, type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--context-output", type=Path)
    args = parser.parse_args()
    studies = []
    references = {}
    for path in args.input:
        descriptor = json.loads(path.read_text())
        loaded = {}
        for key in ("spec", "injuries", "schedule", "memberships"):
            loaded[key] = json.loads(Path(descriptor[key]).read_text())
        snapshot, evidence = load_snapshot(Path(descriptor["snapshot"]))
        context = []
        if descriptor.get("context"):
            with sqlite3.connect(
                f"file:{Path(descriptor['context']).resolve()}?mode=ro", uri=True
            ) as db:
                db.row_factory = sqlite3.Row
                context = [
                    dict(r) for r in db.execute("SELECT * FROM player_game_context")
                ]
        pair = next(p for p in PAIRS if p["pair_id"] == descriptor["pair_id"])
        spec = loaded["spec"]
        games = schedule_games(loaded["schedule"], spec["season"], spec["team_abbr"])
        causal_rows = (
            json.loads(Path(descriptor["causal_rows"]).read_text())
            if descriptor.get("causal_rows")
            else []
        )
        causal_spec = (
            json.loads(Path(descriptor["causal_spec"]).read_text())
            if descriptor.get("causal_spec")
            else None
        )
        studies.append(
            build_study(
                pair,
                spec,
                evidence.rows,
                loaded["injuries"]["rows"],
                games,
                loaded["memberships"],
                context,
                causal_rows,
                causal_spec,
            )
        )
        references[pair["pair_id"]] = {
            "descriptor": digest(descriptor),
            "stats_snapshot": snapshot["sha256"],
        }
    if len({s["pair_id"] for s in studies}) != len(studies):
        raise ValueError("Duplicate study pair")
    # Keep all pair/outcome hypotheses in the correction family, including unavailable slots.
    from statsmodels.stats.multitest import multipletests

    hypotheses = [
        m for s in studies for m in s["metrics"] if m["metric"] in CORE_METRICS
    ]
    pvalues = [(m.get("causal") or {}).get("p_value") for m in hypotheses]
    pvalues = [1.0 if p is None else p for p in pvalues]
    corrected = multipletests(
        pvalues + [1.0] * (len(PAIRS) * len(CORE_METRICS) - len(pvalues)), method="holm"
    )[1]
    for m, value in zip(hypotheses, corrected):
        if m["causal"].get("p_value") is not None:
            m["causal"]["holm_p_value"] = float(value)
            m["causal"]["family_size"] = len(PAIRS) * len(CORE_METRICS)
    stamp = datetime.now(timezone.utc).isoformat()
    document = {
        "version": 1,
        "artifact_type": "multi_metric_studies/v3",
        "kind": "reconstructed",
        "as_of_ts": stamp,
        "captured_at": stamp,
        "rows": [],
        "input_hashes": references,
        "studies": studies,
    }
    document["sha256"] = digest(document)
    append_snapshot(args.output, document)
    if args.context_output:
        rows = [
            {
                "season": r["season"],
                "game_id": r["game_id"],
                "player_id": r["player_id"],
                "teammate_id": r["teammate_id"],
                "teammate_status": r["exposure"],
                "home_away": r["home_away"],
                "rest_days": r.get("rest_days"),
            }
            for s in studies
            for r in s["panel"]
            if r["eligibility"] == "eligible"
        ]
        context_document = {
            "version": 1,
            "kind": "reconstructed",
            "as_of_ts": stamp,
            "captured_at": stamp,
            "rows": rows,
            "input_hashes": {"catalog": document["sha256"]},
        }
        context_document["sha256"] = digest(context_document)
        append_snapshot(args.context_output, context_document)
    print(f"Published {len(studies)} studies to {args.output}")


if __name__ == "__main__":
    main()
