#!/usr/bin/env python3
"""Serve an explicitly authorized weekly PTS candidate on loopback."""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["PYTHON_DOTENV_DISABLED"] = "1"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--authority", type=Path, required=True)
    parser.add_argument("--jobs-state", type=Path)
    parser.add_argument("--port", type=int, default=8785)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    import uvicorn
    from app.config import Settings
    from app.jobs import create_jobs_app
    from app.serving.code_identity import app_code_id
    from app.serving.weekly_authority import WeeklyAuthority

    jobs = (
        create_jobs_app(args.jobs_state, snapshot_root=args.data_root)
        if args.jobs_state is not None
        else None
    )
    settings = Settings(
        project_id="weekly-local",
        gold_dataset="unused",
        metadata_dataset="unused",
        freshness_threshold_hours=1,
        max_search_results=5,
        performance_cache_prewarm_enabled=False,
    )
    app = WeeklyAuthority(
        args.authority, expected_code_id=app_code_id(ROOT / "app")
    ).application(args.data_root, settings=settings, jobs_app=jobs)
    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False)


if __name__ == "__main__":
    main()
