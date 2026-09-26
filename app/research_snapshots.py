"""Immutable, content-verified research snapshots with explicit time semantics."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.agent.teammate_readiness import latest_report, utc, validate_memberships

MAX_SNAPSHOT_BYTES = 50_000_000


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def validate_snapshot(document: dict[str, Any]) -> None:
    if document.get("version") != 1 or document.get("kind") not in (
        "captured",
        "reconstructed",
    ):
        raise ValueError("Unsupported research snapshot")
    if document.get("sha256") != digest(
        {k: v for k, v in document.items() if k != "sha256"}
    ):
        raise ValueError("Research snapshot checksum mismatch")
    cutoff, captured = utc(document["as_of_ts"]), utc(document["captured_at"])
    if cutoff > captured:
        raise ValueError("Snapshot cutoff cannot be in the future")
    if not document.get("input_hashes"):
        raise ValueError("Immutable input references are required")
    seen = set()
    for row in document["rows"]:
        key = (row["season"], row["game_id"], row["player_id"], row.get("teammate_id"))
        if key in seen:
            raise ValueError("Duplicate snapshot context grain")
        seen.add(key)
        if document["kind"] == "captured":
            for stamp in row.get("input_known_at", []):
                if utc(stamp) > min(
                    cutoff, utc(row.get("cutoff_ts", document["as_of_ts"]))
                ):
                    raise ValueError("Input was not known at snapshot cutoff")


def read_snapshot(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        data = handle.read(MAX_SNAPSHOT_BYTES + 1)
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Research snapshot exceeds size limit")
    document = json.loads(data)
    validate_snapshot(document)
    return document


def append_snapshot(path: Path, document: dict[str, Any]) -> bool:
    """Atomic create-only publication. Concurrent identical retries are harmless."""
    validate_snapshot(document)
    encoded = json.dumps(document, sort_keys=True, allow_nan=False).encode()
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Research snapshot exceeds size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".research-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if read_snapshot(path) != document:
                raise ValueError(
                    "Snapshot identity already exists with different content"
                ) from None
            return False
        return True
    finally:
        os.unlink(temporary)


def build_context_snapshot(
    *,
    games,
    reports,
    memberships,
    pairs,
    as_of_ts,
    kind,
    captured_at=None,
    source_status=None,
):
    """Schedule versions may describe future events; late input versions are excluded."""
    validate_memberships(memberships)
    cutoff = utc(as_of_ts)
    captured_at = captured_at or datetime.now(timezone.utc).isoformat()
    rows = []
    for game in games:
        start = game.get("scheduled_start_utc")
        # Captured mode requires actual source acquisition/version timestamps.
        known = [game.get("ingested_at_utc"), game.get("source_updated_at_utc")]
        if kind == "captured" and (
            not all(known) or any(utc(t) > cutoff for t in known)
        ):
            continue
        if game.get("postponed") or not start:
            continue
        game_cutoff = min(cutoff, utc(start) - timedelta(minutes=30))
        if kind == "captured" and any(utc(t) > game_cutoff for t in known):
            continue
        eligible_reports = [
            r
            for r in reports
            if utc(r["report_timestamp_utc"]) <= game_cutoff
            and (kind != "captured" or utc(r["ingested_at_utc"]) <= game_cutoff)
        ]
        for pair in pairs:
            member_rows = []
            for pid in (pair["player_id"], pair["teammate_id"]):
                member = next(
                    (
                        r
                        for r in memberships
                        if r["season"] == game["season"]
                        and r["player_id"] == pid
                        and r["team_abbr"] == game["team_abbr"]
                        and r["valid_from"] <= game["game_date"] < r["valid_to"]
                    ),
                    None,
                )
                if member and kind == "captured":
                    stamps = [member["reviewed_at"], *member["source_published_at"]]
                    if any(len(t) == 10 or utc(t) > game_cutoff for t in stamps):
                        member = None
                member_rows.append(member)
            if not all(member_rows):
                continue
            report = latest_report(eligible_reports, game, pair["teammate_id"], 48)
            selected_times = [
                r["ingested_at_utc"]
                for r in eligible_reports
                if r.get("source_url") in report.get("sources", [])
            ]
            rows.append(
                {
                    **game,
                    **pair,
                    "cutoff_ts": game_cutoff.isoformat(),
                    "cutoff_policy": "before_scheduled_start_minus_30m",
                    "availability": report,
                    "teammate_status": "unknown"
                    if report["status"] == "Unknown"
                    else "conflicting"
                    if report["status"] == "Conflicting"
                    else None,
                    "input_known_at": [t for t in known if t]
                    + selected_times
                    + [
                        t
                        for m in member_rows
                        for t in [m["reviewed_at"], *m["source_published_at"]]
                    ],
                    "membership_evidence": member_rows,
                }
            )
    document = {
        "version": 1,
        "kind": kind,
        "as_of_ts": cutoff.isoformat(),
        "captured_at": captured_at,
        "source_status": source_status or {},
        "input_hashes": {
            "games": digest(games),
            "reports": digest(reports),
            "memberships": digest(memberships),
            "pairs": digest(pairs),
        },
        "rows": rows,
    }
    document["sha256"] = digest(document)
    validate_snapshot(document)
    return document
