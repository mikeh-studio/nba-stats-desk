"""Bounded read-only capture of complete season facts for semantic evaluation."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agent.execution import (
    bounded_timeout,
    record_data_read,
    serialized_warehouse_read,
    timed,
    warehouse_budget,
    warehouse_bytes_billed,
)
from app.agent.semantics import PHASES, Evidence, SemanticError
from app.agent.source_policy import SourcePolicy, capture_sql, denied, get_source_policy
from app.seasons import dataset_for_season, validate_season


def capture_timestamp() -> str:
    """Real retrieval time, independent of the request scope clock."""
    return datetime.now(timezone.utc).isoformat()


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    payload = {k: v for k, v in snapshot.items() if k != "sha256"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@timed("validation")
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
        cohort_complete=snapshot.get("cohort_complete", True),
        position_context=snapshot.get("position_context"),
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

    source_mode = "warehouse"

    def __init__(
        self,
        client: Any,
        *,
        project: str,
        gold_dataset: str = "nba_gold",
        position_context_path: str | None = None,
        max_rows: int = 100_000,
        maximum_bytes_billed: int = 100_000_000,
        source_policy: SourcePolicy | None = None,
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
        self.source_policy = source_policy
        self.position_context_path = position_context_path
        self.client = client
        self.project = project
        self.dataset = gold_dataset
        self.max_rows = max_rows
        self.byte_budget = maximum_bytes_billed

    @property
    def cache_identity(self):
        return (
            type(self).__name__,
            self.project,
            self.dataset,
            self.source_mode,
            self.max_rows,
            self.byte_budget,
            get_source_policy(self.source_policy),
        )

    def source_for_season(self, season: str) -> str:
        return f"{self.project}.{dataset_for_season(self.dataset, season)}.fct_player_game_stats"

    def capture(self, seasons: list[str]) -> dict[str, Any]:
        """Broad all-phase capture; intentionally unchanged for ordinary callers."""
        if get_source_policy(self.source_policy) is not None:
            denied("Broad capture requires an explicit scoped source adapter")
        return self._capture(seasons)

    def capture_scoped(self, seasons: list[str], phases: list[str]) -> dict[str, Any]:
        if (
            not phases
            or len(set(phases)) != len(phases)
            or any(p not in PHASES for p in phases)
        ):
            raise SemanticError("invalid_scope", "Supply distinct canonical phases")
        return self._capture(seasons, tuple(phases))

    @serialized_warehouse_read
    def _capture(self, seasons: list[str], phases=None) -> dict[str, Any]:
        from google.api_core.exceptions import Forbidden, GoogleAPICallError, NotFound
        from google.cloud import bigquery

        if not seasons or len(set(seasons)) != len(seasons):
            raise SemanticError("invalid_scope", "Supply distinct explicit seasons")
        for season in seasons:
            validate_season(season)
        policy = get_source_policy(self.source_policy)
        captured_at = policy.snapshot_at if policy else capture_timestamp()
        sources = [self.source_for_season(s) for s in seasons]
        if policy:
            if policy.source_mode != self.source_mode:
                denied("Source mode differs from the approved policy")
            policy.check_scope(seasons, phases)
            if self.position_context_path:
                denied("Optional position context is outside the scoped source policy")
            if sources != [policy.relation(s) for s in seasons]:
                denied("Source relation differs from the approved relation")
            if (
                self.max_rows != policy.max_rows
                or self.byte_budget != policy.maximum_bytes_billed
            ):
                denied("Source caps differ from the approved policy")
        sql = capture_sql(sources, phases)
        params = [
            bigquery.ScalarQueryParameter("snapshot_at", "TIMESTAMP", captured_at),
            bigquery.ScalarQueryParameter("row_limit", "INT64", self.max_rows + 1),
            *[
                bigquery.ScalarQueryParameter(f"season_{i}", "STRING", s)
                for i, s in enumerate(seasons)
            ],
        ]
        if phases is not None:
            params.append(
                bigquery.ArrayQueryParameter("phases", "STRING", list(phases))
            )
        config = bigquery.QueryJobConfig(
            query_parameters=params,
            maximum_bytes_billed=warehouse_budget(self.byte_budget),
            use_legacy_sql=False,
            use_query_cache=False,
            labels={"purpose": "semantic-evaluation"},
        )
        if policy:
            policy.authorize(sql=sql, config=config, seasons=seasons, phases=phases)
        started = datetime.now(timezone.utc)
        job = None
        failure: Exception | None = None
        rows = None
        read_status = "error"
        submission_timeout = bounded_timeout(
            policy.submission_timeout if policy else 30
        )
        try:
            job = self.client.query(sql, job_config=config, timeout=submission_timeout)
            result = job.result(
                timeout=bounded_timeout(policy.result_timeout if policy else 120)
            )
            rows = [dict(row) for row in result]
            read_status = "ok"
        except Forbidden as exc:
            failure = exc
            raise SemanticError(
                "access_denied", "Historical source access denied"
            ) from exc
        except NotFound as exc:
            failure = exc
            raise SemanticError(
                "unsupported_coverage", "Historical relation unavailable"
            ) from exc
        except (GoogleAPICallError, TimeoutError) as exc:
            failure = exc
            raise SemanticError("source_unavailable", "Historical read failed") from exc
        finally:
            record_data_read(
                kind="warehouse",
                budget_ceiling=config.maximum_bytes_billed,
                status=read_status,
                rows=len(rows) if rows is not None else None,
                bytes_processed=getattr(job, "total_bytes_processed", None),
                bytes_billed=warehouse_bytes_billed(job, failure),
                cache_hit=getattr(job, "cache_hit", None),
            )
        if not rows:
            raise SemanticError("unsupported_coverage", "Empty source snapshot")
        if len(rows) > self.max_rows:
            raise SemanticError("incomplete_evidence", "Oversized source snapshot")
        if any(row.pop("source_row_count", None) != len(rows) for row in rows):
            raise SemanticError("incomplete_evidence", "Source query was truncated")
        for row in rows:
            if row.get("season") not in seasons or (
                phases is not None and row.get("season_type") not in phases
            ):
                raise SemanticError(
                    "invalid_evidence", "Source returned rows outside requested scope"
                )
            if (
                not isinstance(row.get("player_name"), str)
                or not row["player_name"].strip()
            ):
                raise SemanticError(
                    "invalid_evidence", "Source identity name is missing"
                )
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
            "cohort_complete": True,
            "sources": sources,
            "source_timestamp": captured_at,
            "coverage": coverage,
            "rows": rows,
            "source_mode": policy.source_mode if policy else "warehouse",
            "requested_scopes": [[s, p] for s in seasons for p in (phases or PHASES)],
            "capture": {
                "sql": sql,
                "query_id": job.job_id,
                "bytes_processed": job.total_bytes_processed,
                "bytes_billed": job.total_bytes_billed,
                "query_count": 1,
                "latency_ms": round(
                    (datetime.now(timezone.utc) - started).total_seconds() * 1000
                ),
                "maximum_bytes_billed": config.maximum_bytes_billed,
            },
            "limitations": [
                "Completeness verifies warehouse retrieval, not upstream NBA coverage.",
                "Retrospective corrected facts, not historical ingestion knowledge.",
            ],
        }
        if self.position_context_path:
            path = Path(self.position_context_path)
            try:
                if path.stat().st_size > 1_000_000:
                    raise ValueError("size")
                snapshot["position_context"] = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                raise SemanticError(
                    "invalid_evidence", "Configured position context could not be read"
                ) from exc
        snapshot["sha256"] = snapshot_digest(snapshot)
        snapshot_evidence(snapshot)
        return snapshot


class ScopedSemanticSource:
    """Adapt an explicit phase scope to the ordinary warehouse loader protocol."""

    def __init__(self, source: BigQuerySemanticSource, phases):
        if (
            not phases
            or any(p not in PHASES for p in phases)
            or len(set(phases)) != len(phases)
        ):
            raise SemanticError("invalid_scope", "Supply distinct canonical phases")
        self.source = source
        self.phases = tuple(phases)

    @property
    def source_policy(self):
        return self.source.source_policy

    @property
    def cache_identity(self):
        policy = get_source_policy(self.source.source_policy)
        return ("scoped", self.phases, policy, self.source.cache_identity)

    def capture(self, seasons):
        return self.source.capture_scoped(seasons, list(self.phases))
