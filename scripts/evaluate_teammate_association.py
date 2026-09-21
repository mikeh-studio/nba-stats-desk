#!/usr/bin/env python3
"""Evaluate a frozen teammate panel locally; preserve exploratory inference limits."""

import argparse
import hashlib
import json
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import statsmodels  # noqa: E402
from scripts.teammate_association import analyze  # noqa: E402


def report(result):
    def fmt(x):
        return "Unavailable" if x is None else f"{x:.3f}"

    def estimate(row):
        if row["status"] != "estimated_exploratory":
            return row.get("reason", row["status"])
        return fmt(row["adjusted_difference"])

    rows = [
        "# Adjusted teammate association — exploratory report",
        "",
        "**No validated statistical-significance or causal claim is supported by this analysis.**",
        "",
        "Primary outcome: assists per focal-player appearance. Exposure: teammate reported Out with no appearance versus participated.",
        "",
        "Controls: prior opponent win%, rest days, home/away, other players reported Out for Injury/Illness, and calendar-month fixed effects. No same-game minutes control.",
        "",
        f"Complete cases: {len(result['complete_case_game_ids'])}; excluded appearances: {len(result['excluded'])}. Missing fields: {result['missing_counts']}.",
        "",
        f"Same-sample raw difference: **{fmt(result['same_sample_raw_difference'])} assists/game**.",
        "",
        "| Model / sensitivity | Adjusted difference | N | Nominal HC3 95% interval | Nominal episode-cluster 95% interval |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    for name, row in [
        ("Primary", result["primary"]),
        ("Months with both exposures only", result["overlap_only"]),
        ("Secondary: mean game assists/36", result["secondary_mean_game_ast_per36"]),
    ]:
        uncertainty = row.get("nominal_uncertainty", {})
        intervals = [
            " to ".join(fmt(v) for v in uncertainty.get(k, {}).get("nominal_95_ci", []))
            or "Unavailable"
            for k in ("HC3", "cluster")
        ]
        rows.append(
            f"| {name} | {estimate(row)} | {row.get('n', '—')} | {intervals[0]} | {intervals[1]} |"
        )
    rows += [
        "",
        "The nominal intervals are diagnostic approximations. HC3 allows unequal error variance but not serial dependence; the cluster calculation uses exposure stretches with a small-sample correction and G−1 t reference. These stretches are not known to be independent. Few, uneven clusters prevent treating these as validated uncertainty.",
        "",
        "## Calendar support",
        "",
        "| Month | Teammate played | Teammate Out |",
        "| --- | ---: | ---: |",
    ]
    rows.extend(
        f"| {m} | {c.get('0', 0)} | {c.get('1', 0)} |"
        for m, c in result["month_exposure_counts"].items()
    )
    rows += [
        "",
        "## Episode sensitivity",
        "",
        "| Omitted episode | Adjusted difference | Status |",
        "| --- | ---: | --- |",
    ]
    rows.extend(
        f"| {r['omitted_episode']} | {estimate(r)} | {r['status']} |"
        for r in result["leave_episode_out"]
    )
    rows += [
        "",
        "## Covariate balance before adjustment",
        "",
        "| Covariate | Played mean | Out mean | Played / Out ranges |",
        "| --- | ---: | ---: | --- |",
    ]
    rows.extend(
        f"| {c} | {fmt(d['played_mean'])} | {fmt(d['out_mean'])} | {d['ranges']} |"
        for c, d in result["covariate_balance"].items()
    )
    rows += ["", "## Limitations", ""] + [f"- {s}" for s in result["limits"]]
    rows += [
        "",
        "## Method and audit",
        "",
        "[Statsmodels robust covariance documentation](https://www.statsmodels.org/stable/generated/statsmodels.regression.linear_model.OLSResults.get_robustcov_results.html) · [Few-cluster inference reference](https://cameron.econ.ucdavis.edu/research/Cameron_Miller_JHR_2015.pdf)",
        "",
        "See `results.json` for nominal diagnostic p-values, per-game covariates and source evidence; `manifest.json` records input/code hashes, versions, runtime and model costs. Nominal p-values are intentionally not presented as significance badges. Pipeline model tokens and model API cost: **0 / $0**, excluding the assistant conversation.",
    ]
    return "\n".join(rows) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("panel", "injuries", "plan", "output-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    start = time.perf_counter()
    plan = json.loads(args.plan.read_text())
    if plan["primary_outcome"] != "ast" or plan["covariates"] != [
        "opponent_prior_win_pct",
        "rest_days",
        "home",
        "other_reported_injury_out",
        "calendar_month_fixed_effects",
    ]:
        raise ValueError("Plan does not match the implemented fixed specification")
    panel = json.loads(args.panel.read_text())
    injuries = json.loads(args.injuries.read_text())
    result = analyze(panel, injuries["rows"])
    elapsed = time.perf_counter() - start
    args.output_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "inputs": {
            k: {
                "path": str(getattr(args, k).resolve()),
                "sha256": hashlib.sha256(getattr(args, k).read_bytes()).hexdigest(),
            }
            for k in ("panel", "injuries", "plan")
        },
        "code_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (Path(__file__), ROOT / "scripts/teammate_association.py")
        },
        "python": platform.python_version(),
        "statsmodels": statsmodels.__version__,
        "runtime_seconds": elapsed,
        "model_tokens": 0,
        "model_api_cost_usd": 0,
    }
    for name, value in [("results", result), ("manifest", manifest)]:
        (args.output_dir / f"{name}.json").write_text(
            json.dumps(value, indent=2, allow_nan=False) + "\n"
        )
    (args.output_dir / "report.md").write_text(report(result))
    print(
        json.dumps(
            {
                "primary": result["primary"],
                "overlap_only": result["overlap_only"],
                "excluded": result["excluded"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
