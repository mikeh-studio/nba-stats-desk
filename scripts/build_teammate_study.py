#!/usr/bin/env python3
"""Build an auditable local teammate panel from frozen inputs, without model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.agent.semantic_source import load_snapshot  # noqa: E402
from app.agent.teammate_readiness import (  # noqa: E402
    build_panel,
    schedule_games,
    summarize_panel,
)


def render_report(spec, summary, steps, panel=()):
    lines = [
        f"# {spec['name']}: teammate data-readiness report",
        "",
        f"Window: {spec['start']} to {spec['end']} ({spec['season']}, regular season).",
        "",
        "**Descriptive comparison, not a causal effect.**",
        "",
        f"Readiness: {summary['readiness']['status']}.",
        "",
        f"{summary['team_games']} team games; {summary['included_games']} included focal appearances; "
        f"{summary['focal_nonparticipation_games']} focal nonparticipation games retained for audit.",
        "",
        "Exposure classifications: " + json.dumps(summary["exposure_counts"]),
        "",
        "| Metric | Teammate participated | Reported Out, no appearance | Out minus participated |",
        "| --- | ---: | ---: | ---: |",
    ]

    def fmt(value):
        return "Unavailable" if value is None else f"{value:.2f}"

    played, out = (
        summary["groups"][g] for g in ("participated", "reported_out_no_appearance")
    )
    lines.append(
        f"| Games / episodes | {played['games']} / {played['episodes']} | {out['games']} / {out['episodes']} | — |"
    )
    for metric in ("pts", "ast", "min", "fga", "fg3a", "fta", "tov"):
        a, b = (g[metric]["mean"] for g in (played, out))
        delta = b - a if a is not None and b is not None else None
        lines.append(f"| {metric} per game | {fmt(a)} | {fmt(b)} | {fmt(delta)} |")
    for a, b in zip(played["context_metrics"], out["context_metrics"], strict=True):
        if a["key"] in ("min", "fga", "fg3a", "fta", "tov"):
            continue
        delta = (
            b["value"] - a["value"]
            if a["value"] is not None
            and b["value"] is not None
            and not a["missing_component_games"]
            and not b["missing_component_games"]
            else None
        )
        lines.append(
            f"| {a['label']} ({a['unit']}) | {fmt(a['value'])} ({a['valid_games']}/{played['games']}) | {fmt(b['value'])} ({b['valid_games']}/{out['games']}) | {fmt(delta)} |"
        )
    lines += ["", "## Coverage and sensitivity", ""]
    for name, group in summary["groups"].items():
        lines.append(
            f"- {name}: {group['home_games']} home games; opponent win% coverage {group['opponent_prior_win_pct']['valid_games']}/{group['games']}, mean {fmt(group['opponent_prior_win_pct']['mean'])}; rest mean {fmt(group['rest_days']['mean'])} days."
        )
    for metric, result in summary["differences"].items():
        lines.append(
            f"- {metric}: leave-one-episode-out difference range {list(map(lambda v: round(v, 2), result['leave_one_episode_out_range'])) if result['leave_one_episode_out_range'] else None} ({result['sensitivity_comparisons']} valid omissions)."
        )
    lines += ["", summary["uncertainty"], "", "## Limits", ""]
    lines.extend(f"- {limit}" for limit in summary["limitations"])
    lines += [
        "",
        "## Execution cost",
        "",
        "| Step | Seconds | Model tokens | Model API cost |",
        "| --- | ---: | ---: | ---: |",
    ]
    lines.extend(f"| {s['step']} | {s['seconds']:.3f} | 0 | $0 |" for s in steps)
    lines += [
        "",
        "These costs cover this deterministic pipeline only, not the assistant conversation or prior captures.",
        "",
        "Inspect `panel.json` for game-level evidence and `manifest.json` for input hashes.",
    ]
    lines += [
        "",
        "## Game-level review",
        "",
        "| Date | Game | Exposure | Included | Assists | Captured status / evidence |",
        "| --- | --- | --- | --- | ---: | --- |",
    ]
    for row in panel:
        evidence = row["availability"]
        links = ", ".join(f"[PDF]({url})" for url in evidence["sources"])
        assists = (row["outcomes"] or {}).get("ast")
        lines.append(
            f"| {row['game_date']} | {row['game_id']} | {row['exposure']} | {row['included']} | {fmt(assists)} | {evidence['status']} {links} |"
        )
    lines += ["", "## Membership evidence", ""]
    for url in sorted(
        {
            url
            for row in panel
            for member in row["membership_evidence"]
            if member
            for url in member["source_urls"]
        }
    ):
        lines.append(f"- [Roster / transaction source]({url})")
    lines += [
        "",
        "Membership continuity is a manually reviewed assumption within this study window. The manifest preserves the supplied source dates and review basis.",
    ]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "snapshot",
        "injuries",
        "schedule",
        "memberships",
        "spec",
        "context",
        "output-dir",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    started = time.perf_counter()
    stats, _ = load_snapshot(args.snapshot)
    injuries = json.loads(args.injuries.read_text())
    schedule = json.loads(args.schedule.read_text())
    memberships = json.loads(args.memberships.read_text())
    spec = json.loads(args.spec.read_text())
    with sqlite3.connect(
        f"{args.context.resolve().as_uri()}?mode=ro", uri=True
    ) as conn:
        conn.row_factory = sqlite3.Row
        context = [dict(r) for r in conn.execute("select * from player_game_context")]
    # Context must describe the exact input fact population, not an older snapshot.
    actual = {(r["season"], r["game_id"], r["player_id"]): r for r in context}
    if (
        len(actual) != len(stats["rows"])
        or len(context) != len(actual)
        or any(
            any(
                actual.get((r["season"], r["game_id"], r["player_id"]), {}).get(k) != v
                for k, v in r.items()
            )
            for r in stats["rows"]
        )
    ):
        raise ValueError("Context does not match the frozen statistics snapshot")
    steps = [
        {"step": "Load and validate evidence", "seconds": time.perf_counter() - started}
    ]
    started = time.perf_counter()
    games = schedule_games(schedule, spec["season"], spec["team_abbr"])
    panel = build_panel(
        stats["rows"], injuries["rows"], games, memberships, spec, context
    )
    steps.append(
        {
            "step": "Roster and availability panel",
            "seconds": time.perf_counter() - started,
        }
    )
    started = time.perf_counter()
    summary = summarize_panel(panel)
    steps.append(
        {
            "step": "Descriptive comparison and sensitivity",
            "seconds": time.perf_counter() - started,
        }
    )
    manifest = {
        "inputs": {
            name: {
                "path": str(getattr(args, name).resolve()),
                "sha256": hashlib.sha256(getattr(args, name).read_bytes()).hexdigest(),
            }
            for name in (
                "snapshot",
                "injuries",
                "schedule",
                "memberships",
                "spec",
                "context",
            )
        },
        "code": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (
                Path(__file__).resolve(),
                ROOT / "app/agent/teammate_readiness.py",
                ROOT / "scripts/capture_teammate_reports.py",
                ROOT / "dags/nba_pipeline.py",
            )
        },
        "steps": [{**s, "model_tokens": 0, "model_api_cost_usd": 0} for s in steps],
        "warehouse_mutations": 0,
    }
    args.output_dir.mkdir(parents=True, exist_ok=False)
    for name, data in (("panel", panel), ("summary", summary), ("manifest", manifest)):
        (args.output_dir / f"{name}.json").write_text(
            json.dumps(data, indent=2, allow_nan=False) + "\n"
        )
    (args.output_dir / "report.md").write_text(
        render_report(spec, summary, steps, panel)
    )
    print(
        json.dumps(
            {
                "report": str(args.output_dir / "report.md"),
                "games": len(panel),
                "groups": summary["exposure_counts"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
