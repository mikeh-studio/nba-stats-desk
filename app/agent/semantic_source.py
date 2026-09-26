"""Bounded read-only capture of complete season facts for semantic evaluation."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agent.semantics import COMPONENTS, PHASES, Evidence, SemanticError
from app.seasons import dataset_for_season, validate_season


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    payload = {k: v for k, v in snapshot.items() if k != "sha256"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def snapshot_evidence(snapshot: dict[str, Any]) -> Evidence:
    if snapshot.get("sha256") != snapshot_digest(snapshot):
        raise SemanticError("invalid_evidence", "Snapshot checksum mismatch")
    counts = Counter((r["season"], r["season_type"]) for r in snapshot["rows"])
    coverage = snapshot["coverage"]
    expected = {(c["season"], c["phase"]): c["rows"] for c in coverage}
    if dict(counts) != expected or not expected:
        raise SemanticError("incomplete_evidence", "Source counts do not match rows")
    evidence = Evidence(
        rows=snapshot["rows"],
        covered_scopes=frozenset(expected),
        source=", ".join(snapshot["sources"]),
        snapshot_id=snapshot["sha256"],
        data_through={(c["season"], c["phase"]): c["data_through"] for c in coverage},
        complete=True,
    )
    evidence.validate()
    return evidence


def load_snapshot(path: Path) -> tuple[dict[str, Any], Evidence]:
    snapshot = json.loads(path.read_text())
    return snapshot, snapshot_evidence(snapshot)


class BigQuerySemanticSource:
    """Fixed relation/column reads, never agent-authored SQL.

    Capture every player in each requested season at one source timestamp.
    COUNT(*) OVER() detects LIMIT truncation in the same snapshot. Completeness
    means complete warehouse rows, not independently proven NBA source coverage.
    """

    def __init__(
        self,
        client: Any,
        *,
        project: str,
        gold_dataset: str = "nba_gold",
        max_rows: int = 100_000,
        maximum_bytes_billed: int = 100_000_000,
    ):
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,62}", project):
            raise SemanticError("invalid_scope", "Invalid project identifier")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", gold_dataset):
            raise SemanticError("invalid_scope", "Invalid dataset identifier")
        if type(max_rows) is not int or not 1 <= max_rows <= 100_000:
            raise SemanticError("invalid_scope", "max_rows must be 1..100000")
        if (
            type(maximum_bytes_billed) is not int
            or not 1 <= maximum_bytes_billed <= 1_000_000_000
        ):
            raise SemanticError("invalid_scope", "Byte budget must be 1..1000000000")
        self.client = client
        self.project = project
        self.dataset = gold_dataset
        self.max_rows = max_rows
        self.byte_budget = maximum_bytes_billed

    def capture(self, seasons: list[str]) -> dict[str, Any]:
        from google.api_core.exceptions import Forbidden, GoogleAPICallError, NotFound
        from google.cloud import bigquery

        if not seasons or len(set(seasons)) != len(seasons):
            raise SemanticError("invalid_scope", "Supply distinct explicit seasons")
        for season in seasons:
            validate_season(season)
        captured_at = datetime.now(timezone.utc).isoformat()
        sources = [
            f"{self.project}.{dataset_for_season(self.dataset, s)}.fct_player_game_stats"
            for s in seasons
        ]
        columns = ", ".join(
            [
                "season",
                "season_type",
                "game_id",
                "player_id",
                "player_name",
                "game_date",
                "team_abbr",
                "opponent_abbr",
                "home_away",
                *sorted(COMPONENTS),
            ]
        )
        selects = [
            f"SELECT {columns} FROM `{source}` FOR SYSTEM_TIME AS OF @snapshot_at "
            f"WHERE season = @season_{i}"
            for i, source in enumerate(sources)
        ]
        sql = (
            "WITH source AS (" + " UNION ALL ".join(selects) + ") "
            "SELECT *, COUNT(*) OVER() AS source_row_count FROM source "
            "ORDER BY season, game_date, game_id, player_id LIMIT @row_limit"
        )
        params = [
            bigquery.ScalarQueryParameter("snapshot_at", "TIMESTAMP", captured_at),
            bigquery.ScalarQueryParameter("row_limit", "INT64", self.max_rows + 1),
            *[
                bigquery.ScalarQueryParameter(f"season_{i}", "STRING", s)
                for i, s in enumerate(seasons)
            ],
        ]
        config = bigquery.QueryJobConfig(
            query_parameters=params,
            maximum_bytes_billed=self.byte_budget,
            use_legacy_sql=False,
            use_query_cache=False,
            labels={"purpose": "semantic-evaluation"},
        )
        started = datetime.now(timezone.utc)
        try:
            job = self.client.query(sql, job_config=config, timeout=30)
            result = job.result(timeout=120)
            rows = [dict(row) for row in result]
        except Forbidden as exc:
            raise SemanticError(
                "access_denied", "Historical source access denied"
            ) from exc
        except NotFound as exc:
            raise SemanticError(
                "unsupported_coverage", "Historical relation unavailable"
            ) from exc
        except (GoogleAPICallError, TimeoutError) as exc:
            raise SemanticError("source_unavailable", "Historical read failed") from exc
        if not rows or len(rows) > self.max_rows:
            raise SemanticError(
                "incomplete_evidence", "Empty or oversized source snapshot"
            )
        if any(row.pop("source_row_count") != len(rows) for row in rows):
            raise SemanticError("incomplete_evidence", "Source query was truncated")
        for row in rows:
            row["game_date"] = str(row["game_date"])
        coverage = []
        for season in seasons:
            if not any(r["season"] == season for r in rows):
                raise SemanticError("unsupported_coverage", f"No evidence for {season}")
            for phase in PHASES:
                sample = [
                    r
                    for r in rows
                    if r["season"] == season and r["season_type"] == phase
                ]
                if not sample:
                    continue
                coverage.append(
                    {
                        "season": season,
                        "phase": phase,
                        "rows": len(sample),
                        "data_through": max(r["game_date"] for r in sample),
                    }
                )
        snapshot = {
            "version": 1,
            "evidence_kind": "historical",
            "sources": sources,
            "source_timestamp": captured_at,
            "coverage": coverage,
            "rows": rows,
            "capture": {
                "sql": sql,
                "query_id": job.job_id,
                "bytes_processed": job.total_bytes_processed,
                "bytes_billed": job.total_bytes_billed,
                "query_count": 1,
                "latency_ms": round(
                    (datetime.now(timezone.utc) - started).total_seconds() * 1000
                ),
                "maximum_bytes_billed": self.byte_budget,
            },
            "limitations": [
                "Completeness verifies warehouse retrieval, not upstream NBA coverage.",
                "Retrospective corrected facts, not historical ingestion knowledge.",
            ],
        }
        snapshot["sha256"] = snapshot_digest(snapshot)
        snapshot_evidence(snapshot)
        return snapshot
