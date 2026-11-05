"""CLI compatibility entry point for publication contract validation."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json  # noqa: E402

from app.serving.publication_contract import *  # noqa: E402,F403
from app.serving.publication_contract import (  # noqa: E402
    validate_binding,
    validate_manifest,
)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--binding", type=Path)
    args = parser.parse_args()
    document = json.loads(args.manifest.read_text())
    validate_manifest(document)
    if args.binding:
        validate_binding(json.loads(args.binding.read_text()), document)
    print(
        "Publication contract consistent; authority/content verification and runtime acceptance not performed."
    )
