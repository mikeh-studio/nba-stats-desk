#!/usr/bin/env python3
"""Prepare, generate, judge, and render a local reporting-evidence experiment."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import sqlite3
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.reporting_evidence import (  # noqa: E402
    attach_reporting,
    digest,
    prepare_case,
    validate_response,
)
from app.agent.semantic_source import load_snapshot  # noqa: E402
from scripts.reporting_artifacts import load_run, read, save  # noqa: E402
from scripts.reporting_contracts import (  # noqa: E402
    EVALUATOR_PROMPT,
    GENERATOR_PROMPT,
    batch_schema,
    validate_batch,
    validate_evaluation_cases,
)
from scripts.reporting_render import fmt, render_page, report  # noqa: E402
from scripts.reporting_runner import codex_call  # noqa: E402


def prepare(args):
    if args.run_dir.exists():
        raise ValueError("Choose a new run directory")
    snapshot, evidence = load_snapshot(args.snapshot)
    corpus, cases = read(args.corpus), read(args.cases)
    if len(cases) != 10 or len({c["id"] for c in cases}) != 10:
        raise ValueError("This milestone requires exactly 10 distinct evaluation cases")
    context, context_audit = None, None
    if getattr(args, "context_dir", None):
        context_audit = read(args.context_dir / "context-audit.json")
        if context_audit["snapshot_sha256"] != snapshot["sha256"]:
            raise ValueError("Context was built from a different snapshot")
        if (
            context_audit["context_sha256"]
            != hashlib.sha256(
                (args.context_dir / "context.sqlite").read_bytes()
            ).hexdigest()
        ):
            raise ValueError("Context database changed after audit")
        with sqlite3.connect(
            f"file:{args.context_dir / 'context.sqlite'}?mode=ro", uri=True
        ) as db:
            db.row_factory = sqlite3.Row
            context = {
                "rows": [
                    dict(r) for r in db.execute("select * from player_game_context")
                ],
                "reports": [
                    dict(r)
                    for r in db.execute("select * from player_game_reported_status")
                ],
            }
    prepared = [prepare_case(evidence, corpus, case, context=context) for case in cases]
    args.run_dir.mkdir(parents=True)
    save(args.run_dir / "prepared.json", prepared)
    save(args.run_dir / "corpus.json", corpus)
    save(args.run_dir / "cases.json", cases)
    if context_audit:
        save(args.run_dir / "context-audit.json", context_audit)
    save(
        args.run_dir / "manifest.json",
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "snapshot_path": str(args.snapshot.resolve()),
            "snapshot_sha256": snapshot["sha256"],
            "corpus_sha256": digest(corpus),
            "cases_sha256": digest(cases),
            "prepared_sha256": digest(prepared),
            "context_audit_sha256": digest(context_audit) if context_audit else None,
            "case_count": len(cases),
            "git_head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "generator": "gpt-5.6-luna",
            "evaluator": "gpt-5.6-terra",
            "limits": [
                "Ten selected cases are a pilot, not a representative benchmark.",
                "Curated paraphrases do not test full-text extraction or broad corpus retrieval.",
                "One batch per model saves overhead; cases are not independent model sessions.",
                "Historical stats are retrospective; reporting dates are not archived page-version proof.",
                "Automated scores are advisory and await human review.",
            ],
        },
    )
    print(f"Prepared {len(prepared)} cases in {args.run_dir}")


def compact_bundle(bundle):
    """Only the model packet is rounded/compacted; full audit evidence is retained."""
    value = deepcopy(bundle)
    for metric in value["statistics"]["metrics"]:
        if (
            metric["missing_games"] == 0
            and metric.get("previous_missing_games", 0) == 0
        ):
            metric.pop("missing_games", None)
            metric.pop("previous_missing_games", None)
            metric.pop("valid_games", None)
    teammate = value["statistics"].get("teammate")
    if teammate:
        for period in teammate["periods"].values():
            period.pop("game_evidence", None)

    def rounded(v):
        if isinstance(v, float):
            return round(v, 3)
        if isinstance(v, dict):
            return {k: rounded(x) for k, x in v.items()}
        if isinstance(v, list):
            return [rounded(x) for x in v]
        return v

    return rounded(value)


def preview(args):
    """Inspectable deterministic evidence and exact planned model input; no calls."""
    manifest, prepared = load_run(args.run_dir)
    prompt = generation_prompt(prepared)
    (args.run_dir / "generation-preview.txt").write_text(prompt)
    sections, nav = [], []
    e = html.escape
    for p in prepared:
        case, bundle = p["case"], p["bundle"]
        cid = case["id"]
        nav.append(f'<a href="#{cid}">{cid} {e(case["player_name"])}</a>')
        rows = "".join(
            f"<tr><td>{e(m['label'])}<br><small>{e(m['unit'])}; change {e(m['change_unit'])}</small></td><td>{fmt(m['previous'])}</td><td>{fmt(m['current'])}</td><td>{fmt(m['change'])}</td><td>{m['previous_missing_games']} / {m['missing_games']}</td></tr>"
            for m in bundle["statistics"]["metrics"]
        )
        sections.append(
            f'''<section id="{cid}" data-case="{cid}"><div class="eyebrow">{cid} / deterministic evidence preview</div><h2>{e(case["player_name"])}</h2><p>{e(case["question"])}</p><p>{case["previous_start"]}–{case["previous_end"]} → {case["start"]}–{case["end"]}</p><p>Appearances: {bundle["statistics"]["appearances"]["previous"]} → {bundle["statistics"]["appearances"]["current"]}</p><table><thead><tr><th>Metric</th><th>Previous</th><th>Current</th><th>Change</th><th>Missing games<br>Previous / current</th></tr></thead><tbody>{rows}</tbody></table><details><summary>Inspect complete evidence packet</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px">{e(json.dumps(bundle, indent=2))}</pre></details><div class="human"><label>Your decision <select data-field="decision"><option value="">Not reviewed</option><option>Accept</option><option>Revise</option><option>Reject</option></select></label><label>Your notes<textarea data-field="notes" rows="3"></textarea></label></div></section>'''
        )
    page = render_page(
        NAV="".join(nav),
        SECTIONS="".join(sections),
        LIMITS=e(" ".join(manifest["limits"])),
        RUN_ID="preview-" + manifest["prepared_sha256"],
        HEADING="Context milestone: evidence preview",
        DESCRIPTION=f'{len(prepared)} prepared cases with deterministic metrics and context when supplied. Luna generation and Terra evaluation have not run. <a href="generation-preview.txt">Inspect the exact planned Luna input</a>.',
        FOOTNOTE="No new model responses exist yet; this preview uses zero LLM tokens.",
    )
    (args.run_dir / "preview.html").write_text(page)
    print(args.run_dir / "preview.html")


def generation_prompt(prepared):
    return (
        GENERATOR_PROMPT
        + "\nCASE BUNDLES:\n"
        + json.dumps(
            [compact_bundle(p["bundle"]) for p in prepared],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def require_complete_statistics(prepared):
    """Do not spend model tokens on known incomplete metric inputs."""
    gaps = [
        f"{p['case']['id']}:{m['id']}"
        for p in prepared
        for m in p["bundle"]["statistics"]["metrics"]
        if m.get("missing_games", 0) or m.get("previous_missing_games", 0)
    ]
    if gaps:
        raise ValueError(
            "Repair missing statistical inputs before generation: " + ", ".join(gaps)
        )


def generate(args):
    _, prepared = load_run(args.run_dir)
    require_complete_statistics(prepared)
    prompt = generation_prompt(prepared)
    output = codex_call(
        args.run_dir,
        "generation",
        "gpt-5.6-luna",
        "low",
        prompt,
        batch_schema("responses", [p["case"]["id"] for p in prepared]),
    )
    validate_batch(output, "responses", [p["case"]["id"] for p in prepared])
    checks = [
        {"case_id": p["case"]["id"], "errors": validate_response(p["bundle"], r)}
        for p, r in zip(prepared, output["responses"], strict=True)
    ]
    save(args.run_dir / "structural-checks.json", checks)
    save(
        args.run_dir / "ask-payloads.json",
        [
            attach_reporting(p["overview"], p["bundle"], r)
            for p, r in zip(prepared, output["responses"], strict=True)
        ],
    )
    print(
        f"Generated {len(output['responses'])} responses; {sum(bool(c['errors']) for c in checks)} structural failures"
    )


def evaluate(args):
    _, prepared = load_run(args.run_dir)
    responses = read(args.run_dir / "generation.json")
    validate_batch(responses, "responses", [p["case"]["id"] for p in prepared])
    inputs = [
        {
            "bundle": compact_bundle(p["bundle"]),
            "response": r,
            "expectations": p["case"]["expectations"],
            "structural_errors": validate_response(p["bundle"], r),
        }
        for p, r in zip(prepared, responses["responses"], strict=True)
    ]
    output = codex_call(
        args.run_dir,
        "evaluation",
        "gpt-5.6-terra",
        "medium",
        EVALUATOR_PROMPT
        + "\nCASES:\n"
        + json.dumps(inputs, ensure_ascii=False, separators=(",", ":")),
        batch_schema("evaluations", [p["case"]["id"] for p in prepared]),
    )
    validate_evaluation_cases(output, inputs)
    print(
        json.dumps(
            {
                v: sum(r["verdict"] == v for r in output["evaluations"])
                for v in ("pass", "revise", "reject")
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage", choices=("prepare", "preview", "generate", "evaluate", "report")
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--context-dir", type=Path)
    args = parser.parse_args()
    args.run_dir = args.run_dir.resolve()
    if args.stage == "prepare" and not all((args.snapshot, args.corpus, args.cases)):
        parser.error("prepare requires --snapshot, --corpus, and --cases")
    try:
        {
            "prepare": prepare,
            "preview": preview,
            "generate": generate,
            "evaluate": evaluate,
            "report": report,
        }[args.stage](args)
    except (ValueError, KeyError, RuntimeError) as exc:
        print(f"Evidence evaluation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
