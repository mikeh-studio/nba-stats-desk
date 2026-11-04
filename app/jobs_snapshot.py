"""Read-only display of offline PTS snapshot run records."""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.jobs_schedule import slot


def snapshot_job(root, read_json, now=None):
    now = now or datetime.now(timezone.utc)
    root = Path(root)
    runs = root / "reports/weekly-snapshots/runs"
    store = root / "reports/weekly-snapshots/snapshots"
    history, issues = [], []
    if runs.is_symlink():
        issues.append("Snapshot run history is unavailable.")
    elif runs.is_dir():
        for start_path in list(runs.glob("*.started.json"))[:1000]:
            try:
                start = read_json(start_path)
                run_id = start.get("run_id")
                if (
                    not isinstance(run_id, str)
                    or not re.fullmatch(r"[a-f0-9]{32}", run_id)
                    or start_path.name != run_id + ".started.json"
                ):
                    raise ValueError("Invalid run")
                started = datetime.fromisoformat(start["started_at"])
                result_path = runs / (run_id + ".result.json")
                result = read_json(result_path) if result_path.exists() else None
                if result and (
                    result.get("run_id") != run_id
                    or result.get("status") not in ("succeeded", "failed")
                ):
                    raise ValueError("Invalid result")
                finished = (
                    datetime.fromisoformat(result["finished_at"]) if result else None
                )
                status = (
                    {"succeeded": "success", "failed": "failed"}.get(result["status"])
                    if result
                    else "interrupted"
                )
                item = {
                    "id": run_id,
                    "kind": "refresh",
                    "source": "snapshot",
                    "status": status,
                    "reason": None if result else "worker_not_running",
                    "started_at": started.isoformat(),
                    "finished_at": finished.isoformat() if finished else None,
                    "duration_seconds": max(0, (finished - started).total_seconds())
                    if finished
                    else None,
                    "has_log": False,
                    "stages": [],
                    "season": None,
                    "snapshot_id": result.get("snapshot_id")
                    if result and status == "success"
                    else None,
                    "data_through": None,
                    "row_count": None,
                }
                sid = item["snapshot_id"]
                if sid and re.fullmatch(r"[a-f0-9]{64}", sid):
                    manifest = read_json(store / sid / "manifest.json", limit=1048576)
                    if manifest.get("snapshot_id") != sid:
                        raise ValueError("Snapshot identity differs")
                    scope = manifest.get("scope", {})
                    item.update(
                        season=scope.get("season"),
                        data_through=scope.get("end_date"),
                        row_count=manifest.get("row_count"),
                    )
                history.append(item)
            except (OSError, ValueError, TypeError, KeyError):
                issues.append("A snapshot run or manifest could not be read.")
    history.sort(key=lambda x: x["started_at"], reverse=True)
    last = history[0] if history else None
    good = next((x for x in history if x["status"] == "success"), None)
    through = good["data_through"] if good else None
    try:
        age = (
            (now.astimezone().date() - datetime.fromisoformat(through).date()).days
            if through
            else None
        )
    except (ValueError, TypeError):
        age = None
    next_time = (
        slot(now, "America/Los_Angeles").astimezone(ZoneInfo("America/Los_Angeles"))
        + timedelta(days=7)
    ).astimezone(timezone.utc)
    return {
        "id": "weekly-snapshot",
        "name": "Weekly PTS snapshot (offline candidate)",
        "source": "snapshot",
        "description": "Snapshot build records from retained inputs. A successful offline build does not activate app serving.",
        "season": good["season"] if good else None,
        "status": last["status"] if last else "not_run",
        "reason": last["reason"] if last else None,
        "last_checked_at": last["finished_at"] if last else None,
        "last_attempt_at": last["started_at"] if last else None,
        "last_success_at": good["finished_at"] if good else None,
        "last_duration_seconds": last["duration_seconds"] if last else None,
        "attempt_count": 0,
        "scheduler": {"installed": None, "loaded": None, "label": "Not verified"},
        "schedule": {
            "timezone": "America/Los_Angeles",
            "weekday": 6,
            "hour": 8,
            "next_weekly_at": next_time.isoformat(),
            "next_scheduled_run": None,
            "retry_at": None,
            "max_attempts": 3,
        },
        "freshness": {
            "data_through": through,
            "age_days": age,
            "state": "unavailable",
            "observed": [
                {
                    "season": good["season"],
                    "row_count": good["row_count"],
                    "data_through": through,
                }
            ]
            if good
            else [],
            "observed_at": good["finished_at"] if good else None,
        },
        "history": history,
        "coverage_checks": None,
        "game_mapping": None,
        "issues": issues,
    }
