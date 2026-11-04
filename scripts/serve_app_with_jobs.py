#!/usr/bin/env python3
"""Launch the public app with an explicitly configured local Jobs monitor."""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["PYTHON_DOTENV_DISABLED"] = "1"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path)
    parser.add_argument("--port", type=int, default=8785)
    args = parser.parse_args()
    import uvicorn
    from app.jobs import create_jobs_app
    from app.main import app

    if args.snapshot_root is not None:
        from app.serving.local_weekly import LocalWeeklyPTS

        app.state.weekly_pts = LocalWeeklyPTS(args.snapshot_root)
    app.mount(
        "/jobs-monitor",
        create_jobs_app(args.state_dir, snapshot_root=args.snapshot_root),
    )
    app.state.jobs_monitor_enabled = True
    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False)


if __name__ == "__main__":
    main()
