"""Frozen local evaluation artifacts and integrity checks."""

import json
from pathlib import Path

from app.agent.reporting_evidence import digest


def save(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def read(path):
    return json.loads(Path(path).read_text())


def load_run(run_dir):
    manifest, prepared = (
        read(run_dir / "manifest.json"),
        read(run_dir / "prepared.json"),
    )
    if digest(prepared) != manifest["prepared_sha256"]:
        raise ValueError("Prepared evidence changed after freezing")
    return manifest, prepared
