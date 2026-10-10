"""Create a self-contained, escaped, local-only evaluation review page."""

import argparse
import hashlib
import json
from pathlib import Path

from scripts.ask_eval.grading import comparable


def build_review(baseline, candidate, source, notes=None):
    if not comparable(baseline, candidate):
        raise ValueError(
            "Runs use different cases, evidence, grader or execution modes; rerun before comparing."
        )
    if [r["id"] for r in baseline["results"]] != [
        r["id"] for r in candidate["results"]
    ]:
        raise ValueError("Run selections differ")
    data = (
        json.dumps(
            {
                "baseline": baseline,
                "candidate": candidate,
                "source": source,
                "notes": notes or {},
            },
            ensure_ascii=False,
        )
        .replace("<", "\\u003c")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return (
        Path(__file__)
        .with_name("review.html")
        .read_text()
        .replace("/*__DATA__*/", data)
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument("--candidate", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument(
        "--source", type=Path, help="Matching private production source.json"
    )
    p.add_argument(
        "--notes",
        type=Path,
        help="Private interpretation notes; raw scores stay unchanged",
    )
    args = p.parse_args()
    root = Path(__file__).resolve().parents[2]
    baseline = json.loads(args.baseline.read_text())
    candidate = json.loads(args.candidate.read_text())
    source_bytes = (args.source or root / "tests/fixtures/ask/source.json").read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != candidate["source_sha256"]:
        raise ValueError(
            "Source has changed since this run; restore the matching source"
        )
    html = build_review(
        baseline,
        candidate,
        json.loads(source_bytes),
        json.loads(args.notes.read_text()) if args.notes else None,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        stream.write(html)
    print(args.output.resolve())


if __name__ == "__main__":
    main()
