"""Explicit local server compositions; imports do not select a data source."""

import argparse
from pathlib import Path


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    modes = result.add_subparsers(dest="mode", required=True)
    for name in ("app", "jobs", "weekly"):
        mode = modes.add_parser(name)
        mode.add_argument("--port", type=int, default=8785)
        if name in {"app", "jobs"}:
            mode.add_argument("--state-dir", type=Path, required=True)
            mode.add_argument("--snapshot-root", type=Path)
            mode.add_argument(
                "--also-state-dir", action="append", default=[], type=Path
            )
        else:
            mode.add_argument("--data-root", type=Path, required=True)
            mode.add_argument("--authority", type=Path, required=True)
            mode.add_argument("--jobs-state", type=Path)
    return result


def build_application(args):
    from app.jobs import create_jobs_app

    if args.mode in {"app", "jobs"}:
        jobs = create_jobs_app(
            args.state_dir,
            also_state_dirs=args.also_state_dir,
            snapshot_root=args.snapshot_root,
        )
        if args.mode == "jobs":
            return jobs
        from app.main import app

        if args.snapshot_root is not None:
            from app.serving.local_weekly import LocalWeeklyPTS

            app.state.weekly_pts = LocalWeeklyPTS(args.snapshot_root)
        app.mount("/jobs-monitor", jobs)
        app.state.jobs_monitor_enabled = True
        return app

    from app.config import Settings
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
    return WeeklyAuthority(
        args.authority, expected_code_id=app_code_id(Path(__file__).resolve().parent)
    ).application(args.data_root, settings=settings, jobs_app=jobs)


def main(argv=None):
    import os

    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    cli = parser()
    args = cli.parse_args(argv)
    if not 1 <= args.port <= 65535:
        cli.error("port must be between 1 and 65535")
    import uvicorn

    uvicorn.run(
        build_application(args), host="127.0.0.1", port=args.port, proxy_headers=False
    )


if __name__ == "__main__":
    main()
