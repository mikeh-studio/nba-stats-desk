#!/usr/bin/env python3
"""Publish a compact local Ask study artifact from audited offline outputs."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.teammate_readiness import summarize_panel  # noqa: E402
from scripts.teammate_association import COVARIATES, analyze  # noqa: E402


def study_names(spec, panel):
    """Use ID-bound names from the frozen facts, never the free-text study title."""
    names = {}
    for id_key, name_key in (
        ("player_id", "focal_player_name"),
        ("teammate_id", "teammate_name"),
    ):
        if not panel or any(r[id_key] != spec[id_key] for r in panel):
            raise ValueError("Study identity and panel disagree")
        observed = [r.get(name_key) for r in panel]
        if any(not isinstance(name, str) or not name.strip() for name in observed):
            raise ValueError("Missing evidence-backed player name; rebuild the panel")
        unique = {name.strip() for name in observed}
        if len(unique) != 1:
            raise ValueError("Conflicting evidence-backed player names")
        names[name_key] = unique.pop()
    return names


def validate_evidence(spec, summary, result, panel, injuries, plan):
    """Recompute supplied outputs from the verified inputs before publishing."""
    if plan["primary_outcome"] != "ast" or plan["covariates"] != [
        *COVARIATES,
        "calendar_month_fixed_effects",
    ]:
        raise ValueError("Plan does not match the implemented fixed specification")
    if not panel or any(
        r["player_id"] != spec["player_id"]
        or r["teammate_id"] != spec["teammate_id"]
        or r["season"] != spec["season"]
        or r["team_abbr"] != spec["team_abbr"]
        or r.get("max_report_age_hours") != spec["max_report_age_hours"]
        or not spec["start"] <= r["game_date"] <= spec["end"]
        for r in panel
    ):
        raise ValueError("Study spec and panel disagree")
    study_names(spec, panel)

    # JSON normalization accounts for tuple/list representations in memory.
    def canonical(value):
        return json.dumps(value, sort_keys=True, allow_nan=False)

    if canonical(summary) != canonical(summarize_panel(panel)):
        raise ValueError("Summary does not match the verified panel")
    if canonical(result) != canonical(analyze(panel, injuries["rows"])):
        raise ValueError("Results do not match the verified analysis inputs")


def export(spec, summary, result, panel):
    names = study_names(spec, panel)
    primary = result["primary"]
    if (
        primary["status"] != "estimated_exploratory"
        or result["validated_significance"] is not None
    ):
        raise ValueError("Expected an exploratory estimated study")
    original = summary["groups"]
    return {
        "version": 1,
        "study_id": f"{spec['player_id']}-{spec['teammate_id']}-{spec['start']}-{spec['end']}",
        "claim_level": "exploratory_adjusted_association",
        "scope": {
            "focal_player_id": spec["player_id"],
            "focal_player_name": names["focal_player_name"],
            "teammate_id": spec["teammate_id"],
            "teammate_name": names["teammate_name"],
            "season": spec["season"],
            "phase": "Regular Season",
            "start": spec["start"],
            "end": spec["end"],
            "metric": "ast",
        },
        "statistics": {
            "difference_direction": "Reported Out minus participated; positive means higher focal-player assists in the Out group",
            "original_raw_difference": summary["differences"]["ast"][
                "out_minus_participated"
            ],
            "original_played_n": original["participated"]["games"],
            "original_out_n": original["reported_out_no_appearance"]["games"],
            "complete_case_raw_difference": result["same_sample_raw_difference"],
            "adjusted": {
                "difference": primary["adjusted_difference"],
                "played_n": primary["group_n"]["0"],
                "out_n": primary["group_n"]["1"],
                "nominal_cluster_95_ci": primary["nominal_uncertainty"]["cluster"][
                    "nominal_95_ci"
                ],
                "nominal_p": primary["nominal_uncertainty"]["cluster"]["nominal_p"],
                "episodes": primary["episodes"],
                "largest_episode_share": primary["largest_episode_share"],
            },
            "validated_significance": None,
            "excluded_appearances": len(result["excluded"]),
            "month_exposure_counts": result["month_exposure_counts"],
        },
        "controls": primary["parameters"][2:],
        "limitations": result["limits"]
        + [
            "Frozen retrospective study; scheduled start rather than verified actual tipoff.",
            "Membership continuity is manually reviewed for this bounded window.",
        ],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("spec", "summary", "results", "manifest", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    a = p.parse_args()
    manifest = json.loads(a.manifest.read_text())
    for source in manifest["inputs"].values():
        if (
            hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest()
            != source["sha256"]
        ):
            raise ValueError("Analysis input changed since evaluation")
    spec, summary, result = [
        json.loads(x.read_text()) for x in (a.spec, a.summary, a.results)
    ]
    panel = json.loads(Path(manifest["inputs"]["panel"]["path"]).read_text())
    injuries, plan = [
        json.loads(Path(manifest["inputs"][k]["path"]).read_text())
        for k in ("injuries", "plan")
    ]
    validate_evidence(spec, summary, result, panel, injuries, plan)
    bundle = export(spec, summary, result, panel)
    bundle["source_sha256"] = {
        k: hashlib.sha256(getattr(a, k).read_bytes()).hexdigest()
        for k in ("spec", "summary", "results", "manifest")
    }
    with a.output.open("x") as f:
        json.dump(bundle, f, indent=2, allow_nan=False)
    print(a.output)


if __name__ == "__main__":
    main()
