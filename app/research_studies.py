"""Read-only catalog of the three selected multi-metric research studies."""

from __future__ import annotations

from pathlib import Path

from app.research import CORE_METRICS, SHOOTING_METRICS
from app.research_insights import insights, number
from app.research_snapshots import read_snapshot

PAIRS = (
    {
        "pair_id": "lebron-luka",
        "player_id": 2544,
        "player_name": "LeBron James",
        "teammate_id": 1629029,
        "teammate_name": "Luka Doncic",
    },
    {
        "pair_id": "johnson-young",
        "player_id": 1630552,
        "player_name": "Jalen Johnson",
        "teammate_id": 1629027,
        "teammate_name": "Trae Young",
    },
    {
        "pair_id": "brunson-hart",
        "player_id": 1628973,
        "player_name": "Jalen Brunson",
        "teammate_id": 1628404,
        "teammate_name": "Josh Hart",
    },
)


def pending(pair):
    return {
        **pair,
        "status": "coverage_pending",
        "claim_level": "insufficient_evidence",
        "scope": None,
        "metrics": [
            {
                "metric": key,
                "status": "unavailable",
                "reason": "Reviewed evidence has not been published for this pair",
                "descriptive": None,
                "association": None,
                "causal": None,
            }
            for key in (*CORE_METRICS, *SHOOTING_METRICS)
        ],
        "limitations": [
            "A player pair is a research question, not evidence of a causal effect."
        ],
    }


def catalog(path: str | None):
    entries = {p["pair_id"]: pending(p) for p in PAIRS}
    if not path:
        return list(entries.values())
    document = read_snapshot(Path(path))
    if document.get("artifact_type") != "multi_metric_studies/v3":
        raise ValueError("Unsupported research study catalog")
    seen = set()
    for study in document["studies"]:
        pid = study["pair_id"]
        if pid in seen or pid not in entries:
            raise ValueError("Duplicate or unsupported study pair")
        seen.add(pid)
        pair = entries[pid]
        if any(study[k] != pair[k] for k in ("player_id", "teammate_id")):
            raise ValueError("Study roles disagree with selected pair")
        if {m["metric"] for m in study["metrics"]} != set(
            (*CORE_METRICS, *SHOOTING_METRICS)
        ) or len(study["metrics"]) != len(CORE_METRICS) + len(SHOOTING_METRICS):
            raise ValueError("Incomplete metric panel")
        for m in study["metrics"]:
            causal = m.get("causal") or {}
            if causal.get("claim_level") == "causal_estimate_under_assumptions" and (
                not causal.get("assumptions") or not study.get("causal_spec_hash")
            ):
                raise ValueError("Causal claim requires reviewed specification")
        entries[pid] = {
            **{k: v for k, v in study.items() if k != "panel"},
            "catalog_hash": document["sha256"],
        }
    return list(entries.values())


def study_answer(study, metrics=None):
    selected = set(metrics or (*CORE_METRICS, *SHOOTING_METRICS))
    if not selected <= set((*CORE_METRICS, *SHOOTING_METRICS)):
        raise ValueError("Unsupported study metric")
    assessments, highlights, narrative = insights(study, selected)
    by_metric = {a["metric"]: a for a in assessments}
    rows = []
    for m in study["metrics"]:
        if m["metric"] not in selected:
            continue
        d, a, c = (m.get(k) or {} for k in ("descriptive", "association", "causal"))
        assessment = by_metric[m["metric"]]
        uncertainty = assessment["interval"]
        rows.append(
            [
                m.get("label", m["metric"].upper()),
                d.get("participated"),
                d.get("reported_out"),
                d.get("difference"),
                a.get("adjusted_difference"),
                c.get("estimate"),
                m.get("unit", "per game"),
                m.get("status"),
                m.get("reason", ""),
                assessment["significance"],
                " to ".join(number(v) for v in uncertainty)
                if uncertainty
                else "Unavailable",
                assessment["claim"],
                assessment["holm_p_value"],
                " / ".join(str(n) for n in assessment["sample"].get("group_n", []))
                or "Unavailable",
                " / ".join(str(n) for n in assessment["sample"].get("arm_episodes", []))
                or "Unavailable",
                assessment["stability"],
                assessment["practical"],
                assessment["meaningful_change_threshold"],
                assessment["sample"].get("missing_n"),
                assessment["sample"].get("excluded_games"),
                assessment["sample"].get("largest_episode_share"),
                " / ".join(
                    number(v)
                    for v in assessment["sample"].get(
                        "chronological_half_differences", []
                    )
                )
                or "Unavailable",
                assessment["causal_n"],
                assessment["causal_episodes"],
            ]
        )
    # Preserve full precision in the attached study; keep the visible table legible.
    rows = [
        [
            "Unavailable"
            if value is None
            else format(value, ".3g")
            if isinstance(value, float)
            else value
            for value in row
        ]
        for row in rows
    ]
    scope = study.get("scope")
    explanation = (
        f"{scope['season']} regular season, {scope['start']} through {scope['end']}. Observed and adjusted differences compare reported Out with no appearance minus participated. Causal estimates, when supported, use separately verified unavailable minus available exposure."
        if scope
        else "Reviewed inputs have not yet been published for this pair."
    )
    return {
        "answer": f"{study['player_name']} when {study['teammate_name']} is unavailable: {explanation}\n\n{narrative}\n\nAn unavailable causal estimate is not a finding of no effect. Results apply to the specified study population; representativeness beyond it is not established. Practical-size thresholds are product heuristics, not universal basketball or fantasy cutoffs.",
        "research_highlights": highlights,
        "research_assessments": assessments,
        "tables": [
            {
                "title": "Teammate study",
                "columns": [
                    {"key": str(i), "label": label}
                    for i, label in enumerate(
                        (
                            "Stat",
                            "Participated",
                            "Reported Out",
                            "Observed difference",
                            "Adjusted association",
                            "Causal estimate",
                            "Unit",
                            "Evidence status",
                            "Reason",
                            "Statistical evidence",
                            "Pointwise 95% interval",
                            "Interval / test target",
                            "Holm-adjusted p",
                            "Observed games: participated / Out",
                            "Observed episodes: participated / Out",
                            "Stability",
                            "Practical size",
                            "Product threshold (native units)",
                            "Missing observed outcomes",
                            "Excluded scheduled games",
                            "Largest episode share within an observed arm",
                            "Observed difference: first / second chronological half",
                            "Causal eligible games",
                            "Causal episodes",
                        )
                    )
                ],
                "rows": rows,
            }
        ],
        "charts": [],
        "followups": [
            "Is that difference statistically supported?",
            "What about plus-minus?",
            "Does one absence episode explain the result?",
        ],
        "tool_calls": [],
        "clarification_options": [],
        "assumptions": study["limitations"],
        "study": study,
        "study_id": study["pair_id"],
        "study_status": study["status"],
    }
