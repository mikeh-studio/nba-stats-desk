#!/usr/bin/env python3
"""Serve the read-only Jobs page on loopback; no ingestion or dotenv loading."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.jobs import create_jobs_app  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8785)
    parser.add_argument("--also-state-dir", action="append", default=[], type=Path)
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(
        create_jobs_app(args.state_dir, also_state_dirs=args.also_state_dir),
        host="127.0.0.1",
        port=args.port,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
