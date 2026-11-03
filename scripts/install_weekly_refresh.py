#!/usr/bin/env python3
"""Prepare a weekly macOS worker; installation requires a passing scope preflight."""

from __future__ import annotations

import argparse
import json
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.weekly_refresh import refresh, validate_config  # noqa: E402

LABEL = "com.nba-gcp.weekly-refresh"


def launch_agent(config, config_path, python):
    return {
        "Label": LABEL,
        "ProgramArguments": [
            python,
            str(ROOT / "scripts/weekly_refresh.py"),
            "--config",
            str(config_path),
        ],
        "WorkingDirectory": config["state_dir"],
        "RunAtLoad": True,
        "StartCalendarInterval": {
            "Weekday": (config["weekday"] + 1) % 7,
            "Hour": config["hour"],
            "Minute": 0,
        },
        # An hourly local due check recovers login/sleep misses and permits at
        # most two delayed retries. It does not fetch on every hourly wake.
        "StartInterval": 3600,
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "PYTHON_DOTENV_DISABLED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        "StandardOutPath": str(Path(config["state_dir"]) / "worker.out.log"),
        "StandardErrorPath": str(Path(config["state_dir"]) / "worker.err.log"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--season", required=True)
    parser.add_argument("--airflow", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--timezone", default="America/Los_Angeles")
    parser.add_argument("--weekday", type=int, default=6, help="Monday=0, Sunday=6")
    parser.add_argument("--hour", type=int, default=8)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--source", choices=["nba", "espn"], default="nba")
    parser.add_argument("--first-date", help="Explicit initial date for the ESPN path")
    args = parser.parse_args()
    values = dict(
        project=args.project,
        bucket=args.bucket,
        season=args.season,
        repo=str(ROOT),
        airflow=str(args.airflow.resolve()),
        state_dir=str(args.state_dir.resolve()),
        timezone=args.timezone,
        weekday=args.weekday,
        hour=args.hour,
    )
    if args.source == "espn":
        values.update(source="espn", first_date=args.first_date)
    config = validate_config(values)
    directory = args.state_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path = directory / "config.json"
    if config_path.exists() and json.loads(config_path.read_text()) != config:
        raise ValueError(
            "Use a new state directory when changing a prepared installation"
        )
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    config_path.chmod(0o600)
    candidate = directory / (LABEL + ".plist")
    candidate.write_bytes(
        plistlib.dumps(launch_agent(config, config_path, sys.executable))
    )
    print("Prepared weekly worker:", candidate)
    if not args.install:
        return 0
    preflight = refresh(config, check_only=True)
    if preflight["status"] != "preflight_passed":
        print(json.dumps(preflight))
        return 2
    if config.get("source") == "espn" and not preflight.get("last_success_at"):
        print("Run and validate an initial ESPN refresh before enabling its schedule.")
        return 2
    destination = Path.home() / "Library/LaunchAgents" / candidate.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Preserve an existing installation so a changed schedule is reversible.
    if destination.exists():
        destination.with_suffix(".plist.previous").write_bytes(destination.read_bytes())
    destination.write_bytes(candidate.read_bytes())
    import os

    domain = "gui/" + str(os.getuid())
    subprocess.run(
        ["launchctl", "bootout", domain + "/" + LABEL], check=False, capture_output=True
    )
    subprocess.run(["launchctl", "enable", domain + "/" + LABEL], check=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(destination)], check=True)
    # Prevent the superseded daily scheduler from returning on the next login.
    subprocess.run(
        ["launchctl", "bootout", domain + "/com.nba-gcp.airflow-scheduler"],
        check=False,
        capture_output=True,
    )
    subprocess.run(
        ["launchctl", "disable", domain + "/com.nba-gcp.airflow-scheduler"],
        check=True,
    )
    print("Installed", LABEL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
