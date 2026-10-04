"""Read-only catalog of the three selected multi-metric research studies."""

from __future__ import annotations

import logging
from pathlib import Path

from app.agent.comparison_detail import comparison_detail, relative_change
from app.agent.performance_overview import scoped_identity_profile
from app.agent.visualization import study_charts
from app.research import CORE_METRICS, SHOOTING_METRICS
from app.research_insights import insights
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
            "comparison_detail": comparison_detail(study),
        }
    return list(entries.values())


def study_answer(study, metrics=None):
    selected = set(metrics or (*CORE_METRICS, *SHOOTING_METRICS))
    if not selected <= set((*CORE_METRICS, *SHOOTING_METRICS)):
        raise ValueError("Unsupported study metric")
    assessments, highlights, narrative = insights(study, selected)

    limited = any(
        a["estimate"] is not None
        and a["significance"] != "statistically supported (exploratory)"
        for a in assessments
    )
    logging.getLogger(__name__).log(
        logging.WARNING if limited else logging.INFO,
        "Teammate study assessment: %s",
        {
            "study_id": study["pair_id"],
            "catalog_hash": study.get("catalog_hash"),
            "metrics": [
                {k: a[k] for k in ("metric", "significance", "stability", "claim")}
                for a in assessments
            ],
        },
    )
    rows = []
    for m in study["metrics"]:
        if m["metric"] not in selected:
            continue
        d = m.get("descriptive") or {}
        rows.append(
            [
                m.get("label", m["metric"].upper()),
                *[
                    "Not available" if d.get(k) is None else f"{d[k]:.1f}"
                    for k in ("participated", "reported_out")
                ],
                "Not available"
                if d.get("difference") is None
                else "0.0"
                if round(d["difference"], 1) == 0
                else f"{d['difference']:+.1f}",
                m.get("unit", "per game"),
                f"{relative_change(m):+.1f}%"
                if relative_change(m) is not None
                else "—",
            ]
        )
    scope = study.get("scope")
    phase_label = {
        "Regular Season": "regular season",
        "Playoffs": "playoffs",
        "Both": "regular season and playoffs",
    }.get((scope or {}).get("phase", "Regular Season"))
    if study.get("coverage", {}).get("excluded_focal_appearances", 0):
        narrative += "\n\nSome games could not be classified from the available teammate-status records and are excluded from this comparison."
    if (study.get("comparison_detail") or {}).get("outside_teammate_window"):
        narrative += "\n\nThis comparison includes only games while they were teammates; appearances outside that roster overlap are excluded."
    scope_line = (
        f"{scope['season']} {phase_label} · {scope['start']} through {scope['end']}."
        if scope
        else ""
    )
    return {
        "answer": narrative + ("\n\n" + scope_line if scope_line else ""),
        "player_profile": scoped_identity_profile(study, [], scope) if scope else None,
        "research_highlights": highlights,
        "comparison_explorer": {
            "teammate": study["teammate_name"],
            "metrics": [
                {
                    "key": m["metric"],
                    "label": m.get("label", m["metric"].upper()),
                    "unit": "%"
                    if m.get("unit") == "percentage points"
                    else m.get("unit", "per game"),
                    "relative_change": relative_change(m),
                    "both": (m.get("descriptive") or {}).get("participated"),
                    "out": (m.get("descriptive") or {}).get("reported_out"),
                    "difference": (m.get("descriptive") or {}).get("difference"),
                }
                for m in study["metrics"]
                if m["metric"] in selected
            ],
            "detail": study.get("comparison_detail"),
        },
        "tables": [
            {
                "title": "Performance comparison",
                "columns": [
                    {"key": str(i), "label": label}
                    for i, label in enumerate(
                        (
                            "Stat",
                            "Both played",
                            f"{study['teammate_name']} out",
                            "Difference (out − both played)",
                            "Unit",
                            "Change vs. both played",
                        )
                    )
                ],
                "rows": rows,
            }
        ],
        "charts": [],
        "_chart_candidates": study_charts(study, selected, highlights),
        "followups": ["What about plus-minus?", "How consistent was that pattern?"],
        "tool_calls": [],
        "clarification_options": [],
        "assumptions": [
            "Relative change uses unrounded averages with both played as the baseline; it is omitted for shooting percentages, plus-minus, and zero or negative baselines. Per-36 uses total production divided by total minutes, not an average of game-level rates. It is a descriptive rate, not an adjustment for opponent or role.",
            "Both played means both appeared in the same team game; it does not measure shared court time.",
            "Out means the teammate was reported Out and did not appear; this does not establish an injury cause.",
        ],
        "study_id": study["pair_id"],
        "study_status": study["status"],
    }
