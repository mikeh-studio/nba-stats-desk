"""Render a separate, auditable step review without making new model calls."""

from __future__ import annotations

import html
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path

from app.agent.reporting_evidence import digest, validate_response
from app.agent.semantic_source import load_snapshot


def read(path):
    return json.loads(Path(path).read_text())


def measured_usage(run_dir, stage):
    """Reconcile saved metadata with completed-turn events; missing is unknown."""
    path = run_dir / f"{stage}-metadata.json"
    if not path.exists():
        return {"status": "unavailable", "total": None, "checks": ["No saved metadata"]}
    metadata = read(path)
    if metadata.get("composition") == "concatenate_responses":
        parts = metadata["parts"]
        if not parts or any(
            Path(p["run_dir"]).resolve() == Path(run_dir).resolve()
            and p["stage"] == stage
            for p in parts
        ):
            raise ValueError("Invalid composite usage references")
        keys = [(str(Path(p["run_dir"]).resolve()), p["stage"]) for p in parts]
        if len(set(keys)) != len(keys) or any(
            read(Path(p["run_dir"]) / f"{p['stage']}-metadata.json").get("composition")
            for p in parts
        ):
            raise ValueError("Duplicate or nested composite usage references")
        usage = [measured_usage(Path(p["run_dir"]), p["stage"]) for p in parts]
        responses = [
            r
            for p in parts
            for r in read(Path(p["run_dir"]) / f"{p['stage']}.json")["responses"]
        ]
        if responses != read(run_dir / f"{stage}.json")["responses"]:
            raise ValueError("Composite answers differ from recorded model outputs")
        if any(u["status"] != "measured" for u in usage):
            raise ValueError("Composite usage contains an unmeasured call")
        return {
            "status": "measured",
            "model": usage[0]["model"],
            **{
                k: sum(u[k] for u in usage)
                if all(u[k] is not None for u in usage)
                else None
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cached_input_tokens",
                    "reasoning_output_tokens",
                    "total",
                )
            },
            "duration_seconds": sum(u["duration_seconds"] for u in usage),
            "completed_turns": sum(u["completed_turns"] for u in usage),
            "checks": [check for u in usage for check in u["checks"]],
            "per_response_tokens": None,
            "attribution": "Two recorded calls: first returned one case; second supplied the other nine. Both costs included.",
            "parts": parts,
        }
    events = [
        json.loads(line)
        for line in (run_dir / f"{stage}-events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    observed = [r["usage"] for r in events if r.get("type") == "turn.completed"]
    if not observed or observed != metadata.get("usage"):
        raise ValueError(f"{stage}: metadata and CLI usage events disagree")
    for row in observed:
        if any(
            type(row.get(k)) is not int or row[k] < 0
            for k in ("input_tokens", "output_tokens")
        ):
            raise ValueError(f"{stage}: incomplete token measurement")
    totals = {
        k: sum(r[k] for r in observed)
        if all(k in r and r[k] is not None for r in observed)
        else None
        for k in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "reasoning_output_tokens",
        )
    }
    final_messages = [
        r["item"]["text"]
        for r in events
        if r.get("type") == "item.completed"
        and r.get("item", {}).get("type") == "agent_message"
    ]
    checks = []
    if not final_messages or json.loads(final_messages[-1]) != read(
        run_dir / f"{stage}.json"
    ):
        checks.append("Saved output differs from the final CLI message")
    if (
        digest((run_dir / f"{stage}-prompt.txt").read_text())
        != metadata["prompt_sha256"]
    ):
        checks.append("Saved prompt hash differs from execution metadata")
    if metadata.get("exit_code") != 0 or metadata.get("tool_items"):
        checks.append("CLI exit or tool-use check failed")
    elapsed = (
        datetime.fromisoformat(metadata["ended_at"])
        - datetime.fromisoformat(metadata["started_at"])
    ).total_seconds()
    return {
        "status": "measured",
        "model": metadata["requested_model"],
        **totals,
        "total": totals["input_tokens"] + totals["output_tokens"],
        "duration_seconds": round(elapsed, 2),
        "completed_turns": len(observed),
        "checks": checks,
        "per_response_tokens": None,
        "attribution": "One batch for ten cases; individual response usage was not measured.",
    }


def audit_statistics(snapshot, prepared):
    """Recompute rates from raw rows, independently of the overview builder."""
    audits = []
    for item in prepared:
        case, bundle = item["case"], item["bundle"]
        rows = [
            r
            for r in snapshot["rows"]
            if r["player_id"] == case["player_id"]
            and r["season"] in case["seasons"]
            and r["season_type"] == case["phase"]
        ]
        periods = {
            "current": [
                r for r in rows if case["start"] <= r["game_date"] <= case["end"]
            ],
            "previous": [
                r
                for r in rows
                if case["previous_start"] <= r["game_date"] <= case["previous_end"]
            ],
        }
        errors, reference = [], []
        for name, sample in periods.items():
            if len(sample) != bundle["statistics"]["appearances"][name]:
                errors.append(f"{name}: appearance count mismatch")
        for metric in bundle["statistics"]["metrics"]:
            key = metric["id"].removeprefix("S_")
            expected, complete = {}, {}
            for name, sample in periods.items():
                ratios = {
                    "fg_pct": (("fgm", "fga"), lambda t: (t["fgm"], t["fga"], 100)),
                    "fg3_pct": (
                        ("fg3m", "fg3a"),
                        lambda t: (t["fg3m"], t["fg3a"], 100),
                    ),
                    "ft_pct": (("ftm", "fta"), lambda t: (t["ftm"], t["fta"], 100)),
                    "efg_pct": (
                        ("fgm", "fg3m", "fga"),
                        lambda t: (t["fgm"] + 0.5 * t["fg3m"], t["fga"], 100),
                    ),
                    "ts_pct": (
                        ("pts", "fga", "fta"),
                        lambda t: (t["pts"], 2 * (t["fga"] + 0.44 * t["fta"]), 100),
                    ),
                    "pts_per36": (("pts", "min"), lambda t: (t["pts"], t["min"], 36)),
                    "ast_per36": (("ast", "min"), lambda t: (t["ast"], t["min"], 36)),
                }
                fields = ratios[key][0] if key in ratios else (key,)
                valid = [r for r in sample if all(r.get(f) is not None for f in fields)]
                totals = {f: math.fsum(r[f] for r in valid) for f in fields}
                if key in ratios:
                    numerator, denominator, scale = ratios[key][1](totals)
                    expected[name] = (
                        scale * numerator / denominator
                        if valid and denominator
                        else None
                    )
                else:
                    expected[name] = totals[key] / len(valid) if valid else None
                complete[name] = len(valid) == len(sample)
            expected["change"] = (
                expected["current"] - expected["previous"]
                if (
                    all(v is not None for v in expected.values())
                    and all(complete.values())
                )
                else None
            )
            for name, value in expected.items():
                actual = metric[name]
                if (value is None) != (actual is None) or (
                    value is not None
                    and actual is not None
                    and not math.isclose(value, actual, abs_tol=1e-9)
                ):
                    errors.append(f"{key}.{name}: expected {value}, got {actual}")
            reference.append({"metric": key, **expected})
        audits.append(
            {"case_id": case["id"], "errors": errors, "independent_rates": reference}
        )
    return audits


def build_step_audit(run_dir, manifest, prepared, responses, evaluations):
    snapshot, _ = load_snapshot(Path(manifest["snapshot_path"]))
    if snapshot["sha256"] != manifest["snapshot_sha256"]:
        raise ValueError("Snapshot differs from the run manifest")
    corpus, cases = read(run_dir / "corpus.json"), read(run_dir / "cases.json")
    if (
        digest(corpus) != manifest["corpus_sha256"]
        or digest(cases) != manifest["cases_sha256"]
    ):
        raise ValueError("Frozen corpus or cases changed")
    usage = {
        stage: measured_usage(run_dir, stage) for stage in ("generation", "evaluation")
    }
    stats = audit_statistics(snapshot, prepared)
    structures = [
        {"case_id": p["case"]["id"], "errors": validate_response(p["bundle"], r)}
        for p, r in zip(prepared, responses, strict=True)
    ]
    retrieval = [
        {
            "case_id": p["case"]["id"],
            "question": p["case"]["question"],
            "selected": p["bundle"]["reporting"]["passages"],
            "excluded": p["retrieval_audit"]["excluded"],
            "reporting_mode": p["case"]["reporting_mode"],
            "cutoff": p["case"]["reporting_cutoff"],
        }
        for p in prepared
    ]
    eligible_cases = sum(bool(r["selected"]) for r in retrieval)
    failures = sum(bool(r["errors"]) for r in structures)
    counts = dict(Counter(r["verdict"] for r in evaluations))
    steps = [
        dict(
            id="S01",
            title="Question and scope",
            tokens=None,
            status="Human review needed",
            result=f"{len(cases)} curated questions with explicit identities, periods, and evaluation expectations.",
            criterion="Does the selection represent the questions you want Ask to answer? Are comparison dates and player identities appropriate?",
            limitation="Cases were authored during setup. Setup token usage is outside the recorded experiment; no live question planner was evaluated.",
            inputs="Research goal and chosen historical scenarios",
            outputs="Frozen questions and scope contracts",
            artifacts=["cases.json", "manifest.json"],
            details=cases,
        ),
        dict(
            id="S02",
            title="Warehouse and statistics",
            tokens=0,
            status="Checks passed"
            if not any(s["errors"] for s in stats)
            else "Needs revision",
            result=f"Independently recalculated {len(prepared[0]['bundle']['statistics']['metrics'])} metrics per case for {len(stats)} cases from {len(snapshot['rows']):,} frozen player-game rows.",
            criterion="Do the rates, samples, and comparison windows support the requested analysis?",
            limitation="Arithmetic and snapshot integrity checks do not independently establish upstream NBA completeness. No LLM is invoked here.",
            inputs="Frozen BigQuery rows and case scopes",
            outputs="Governed rates, differences, counts, and team context",
            artifacts=["prepared.json"],
            details={"warehouse_capture": snapshot["capture"], "case_checks": stats},
        ),
        dict(
            id="S03",
            title="Reporting corpus",
            tokens=None,
            status="Human review needed",
            result=f"{len(corpus)} attributed, curator-written source summaries; corpus checksum matches the frozen run.",
            criterion="Do summaries faithfully represent the linked reporting? Is source diversity and context sufficient?",
            limitation="Source selection and paraphrasing happened during setup; their model/tool usage was not captured in this run. Summary quality has not been independently judged against full articles.",
            inputs="Manually inspected publisher pages",
            outputs="Short attributed paraphrases and dates",
            artifacts=["corpus.json"],
            details=corpus,
        ),
        dict(
            id="S04",
            title="Retrieval",
            tokens=0,
            status="Coverage gaps",
            result=f"{eligible_cases}/{len(cases)} cases received reporting; {len(cases) - eligible_cases} have no eligible reporting.",
            criterion="Is the selected evidence relevant, and are exclusion reasons appropriate? Inspect the cutoff case and missing-reporting case.",
            limitation="Filtering uses player, event dates, and publication/update cutoff plus lexical ranking. There is no independently labeled relevance set or measured retrieval recall.",
            inputs="Case scopes and curated corpus",
            outputs="Selected passages and exclusion reasons for every case",
            artifacts=["prepared.json"],
            details=retrieval,
        ),
        dict(
            id="S05",
            title="Luna response generation",
            tokens=usage["generation"]["total"],
            status="Review original answers",
            result=f"{len(responses)} original responses retained. "
            + (
                "Prompt and output match saved CLI evidence."
                if not usage["generation"]["checks"]
                else "Audit checks need attention."
            ),
            criterion="Does each answer synthesize its evidence accurately, directly, and without unsupported explanations?",
            limitation="One batch contains all cases. Cross-case contamination remains a risk. Token usage includes CLI context overhead, not only the prompt file.",
            inputs="Per-case statistics and eligible reporting",
            outputs="Structured claims with evidence IDs",
            artifacts=[
                "generation-prompt.txt",
                "generation-schema.json",
                "generation.json",
                "generation-metadata.json",
                "generation-events.jsonl",
            ],
            details={"usage": usage["generation"], "responses": responses},
        ),
        dict(
            id="S06",
            title="Citation and fallback checks",
            tokens=0,
            status="Needs revision" if failures else "Checks passed",
            result=f"{len(cases) - failures}/{len(cases)} responses pass structural validation; {failures} trigger the deterministic fallback.",
            criterion="Are claim types and citation IDs valid? Does fallback preserve usable statistics?",
            limitation="These checks establish citation structure only, not factual accuracy or whether a passage supports its claim.",
            inputs="Original claims and case-specific evidence IDs",
            outputs="Validation findings and protected Ask-shaped payloads",
            artifacts=["structural-checks.json", "ask-payloads.json"],
            details=structures,
        ),
        dict(
            id="S07",
            title="Terra evaluation",
            tokens=usage["evaluation"]["total"],
            status="Advisory results",
            result=", ".join(f"{n} {label}" for label, n in counts.items())
            + ". Original answers were not repaired or regenerated.",
            criterion="Do you agree with the judge's findings, severity, and scores? Check passed answers as well as flagged ones.",
            limitation="Terra sees curated summaries, not full publisher articles. This judge is not an independent source-fidelity audit or a calibrated quality guarantee.",
            inputs="Original responses, evidence, expectations, and structural findings",
            outputs="Five dimension scores and concrete revision suggestions",
            artifacts=[
                "evaluation-prompt.txt",
                "evaluation-schema.json",
                "evaluation.json",
                "evaluation-metadata.json",
                "evaluation-events.jsonl",
            ],
            details={"usage": usage["evaluation"], "evaluations": evaluations},
        ),
        dict(
            id="S08",
            title="Your final review",
            tokens=0,
            status="Awaiting you",
            result="Response review and step review have separate notes and export files.",
            criterion="Which steps are acceptable, which need changes, and is the narrative useful enough to integrate into Ask?",
            limitation="Zero LLM tokens for rendering and human review. Browser notes are not automatically read back into the experiment.",
            inputs="Answers, supporting evidence, and step audits",
            outputs="Your decisions and exported feedback",
            artifacts=["review.html", "review.md"],
            details={"human_review": "pending; use your saved or exported notes"},
        ),
    ]
    context_path = run_dir / "context-audit.json"
    if context_path.exists():
        context = read(context_path)
        if digest(context) != manifest.get("context_audit_sha256"):
            raise ValueError("Context audit changed after freezing")
        context_steps = [
            dict(
                id=f"C{i:02}",
                title=row["model"],
                tokens=0,
                status="Locally validated",
                result=f"{row['rows']:,} rows; {row['seconds']} seconds local compute; zero LLM tokens.",
                criterion="Is the grain correct, are date cutoffs appropriate, and is coverage sufficient?",
                limitation="Executed the dbt SELECT locally over frozen data. Not a deployed warehouse model; local compute is not a measured cloud bill.",
                inputs="Frozen statistics and historical injury reports",
                outputs="Derived context relation",
                artifacts=["context-audit.json"],
                details=context if i == 1 else row,
            )
            for i, row in enumerate(context["steps"], 1)
        ]
        steps[2:2] = context_steps
    measured = [u["total"] for u in usage.values()]
    return {
        "run_id": manifest["prepared_sha256"],
        "steps": steps,
        "measured_model_tokens": sum(t for t in measured if t is not None),
        "all_model_usage_available": all(t is not None for t in measured),
        "token_scope": "Recorded generation and evaluation only. Excludes setup, source curation, CLI smoke tests, implementation, and this step-audit authoring.",
        "cost_note": "Tokens are usage units, not a dollar bill. No monetary cost is inferred from CLI subscription usage.",
        "accounting": "Total = input + output. Cached input and reported reasoning are displayed as details and are not added again. Per-case token allocation is unavailable for batched calls.",
    }


def render_step_review(run_dir, manifest, prepared, responses, evaluations, template):
    audit = build_step_audit(run_dir, manifest, prepared, responses, evaluations)
    e = html.escape
    nav, cards, token_rows = [], [], []
    for step in audit["steps"]:
        sid = step["id"]
        tokens = (
            ("Unmetered setup" if sid in ("S01", "S03") else "Unavailable")
            if step["tokens"] is None
            else f"{step['tokens']:,}"
        )
        usage = (
            step["details"].get("usage", {})
            if isinstance(step["details"], dict)
            else {}
        )
        input_tokens = f"{usage['input_tokens']:,}" if "input_tokens" in usage else "—"
        output_tokens = (
            f"{usage['output_tokens']:,}" if "output_tokens" in usage else "—"
        )
        nav.append(f'<a href="#{sid}">{sid} {e(step["title"])}</a>')
        token_rows.append(
            f'<tr><td><a href="#{sid}">{e(step["title"])}</a></td><td>{input_tokens}</td><td>{output_tokens}</td><td>{tokens}</td></tr>'
        )
        links = " · ".join(
            f'<a href="{e(name, quote=True)}" target="_blank" rel="noopener">{e(name)}</a>'
            for name in step["artifacts"]
        )
        details = e(json.dumps(step["details"], indent=2, ensure_ascii=False))
        cards.append(
            f'''<section id="{sid}" data-case="{sid}"><div class="eyebrow">{sid} / {e(step["status"])}</div><h2>{e(step["title"])}</h2><p class="question">{e(step["result"])}</p><p><strong>Tokens:</strong> {tokens}</p><p><strong>Input:</strong> {e(step["inputs"])}<br><strong>Output:</strong> {e(step["outputs"])}</p><p><strong>Review this:</strong> {e(step["criterion"])}</p><p class="muted">{e(step["limitation"])}</p><details><summary>Inspect step evidence and case results</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px">{details}</pre></details><p style="overflow-wrap:anywhere">{links}</p><div class="human"><label>Your decision <select data-field="decision"><option value="">Not reviewed</option><option>Accept</option><option>Revise</option><option>Reject</option></select></label><label>Your notes <textarea data-field="notes" rows="3" placeholder="What worked? What should change?"></textarea></label></div></section>'''
        )
    total_label = (
        "Measured model tokens"
        if audit["all_model_usage_available"]
        else "Known model-token subtotal"
    )
    overview = f"""<section><div class="eyebrow">Run accounting</div><h2>{audit["measured_model_tokens"]:,} {total_label.lower()}</h2><p>Inspect and review each step separately. This page adds deterministic audits to existing saved results; it makes no new model calls.</p><table><thead><tr><th>Step</th><th>Input</th><th>Output</th><th>Total LLM tokens</th></tr></thead><tbody>{"".join(token_rows)}</tbody></table><p>{e(audit["token_scope"])}</p><p>{e(audit["cost_note"])}</p><p class="muted">{e(audit["accounting"])}</p><p><a href="review.html">Open the 10-response review</a> · <a href="step-audit.json">Download step audit JSON</a></p></section>"""
    page = template.replace(
        "Can the answer earn your trust?", "Review the work behind each answer."
    )
    old_description = page.split("</h1><p>", 1)[1].split("</p>", 1)[0]
    page = page.replace(
        old_description,
        f"{len(audit['steps'])} separately reviewable steps covering scope, statistics, context when supplied, sources, retrieval, generation, citation checks, evaluation, and your decision. Each includes evidence, limitations, and token accounting.",
    )
    page = page.replace(
        "NBA Stats Desk · Evidence review", "NBA Stats Desk · Step review"
    )
    page = page.replace("nba-evidence-review-", "nba-evidence-step-review-").replace(
        "nba-evidence-human-review.json", "nba-evidence-step-review.json"
    )
    page = (
        page.replace("__NAV__", "".join(nav))
        .replace("__SECTIONS__", overview + "".join(cards))
        .replace("__LIMITS__", e(audit["token_scope"]))
        .replace("__RUN_ID__", audit["run_id"])
    )
    (run_dir / "step-review.html").write_text(page)
    (run_dir / "step-audit.json").write_text(
        json.dumps(audit, indent=2, ensure_ascii=False) + "\n"
    )
    return audit
