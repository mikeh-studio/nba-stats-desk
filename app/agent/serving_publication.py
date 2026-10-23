"""Offline publication contract validation against separately trusted source evidence.

No pointer is published and no serving route is enabled here. A self-consistent
manifest is not proof of source completeness; validation requires the captured
source and its independently supplied expected snapshot ID.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

import jsonschema

from app.agent.semantic_source import snapshot_evidence
from app.agent.semantics import Evidence, Query, SemanticError, load_contract
from app.agent.serving_contract import MetricSummary, metric_matrix, summarize_rows
from app.seasons import season_bounds

VERSION = "ask_serving_publication/1"
POLICY = "ask_serving_scope/1"
MAX_SOURCE_AGE_SECONDS = 300


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _summary_records(summaries: Sequence[MetricSummary]) -> list[dict[str, Any]]:
    return sorted(
        (asdict(s) for s in summaries),
        key=lambda s: (s["season"], s["phase"], s["player_id"], s["metric"]),
    )


def _scopes(source: Evidence) -> list[dict[str, Any]]:
    output = []
    for season, phase in sorted(source.covered_scopes):
        rows = [
            r for r in source.rows if (r["season"], r["season_type"]) == (season, phase)
        ]
        if not rows:
            raise SemanticError(
                "invalid_evidence", "Declared publication scope is empty"
            )
        players = sorted({r["player_id"] for r in rows})
        membership = sorted((r["game_id"], r["player_id"]) for r in rows)
        start, end = season_bounds(season)
        through = source.data_through[(season, phase)]
        if not start <= date.fromisoformat(through) <= end:
            raise SemanticError("invalid_evidence", "Source date outside season")
        if any(
            not start <= date.fromisoformat(str(r["game_date"])) <= end for r in rows
        ):
            raise SemanticError("invalid_evidence", "Appearance outside season")
        output.append(
            dict(
                season=season,
                phase=phase,
                start=str(start),
                end=str(end),
                data_through=through,
                player_count=len(players),
                player_ids=players,
                player_digest=digest(players),
                appearance_count=len(rows),
                appearance_digest=digest(membership),
            )
        )
    return output


def candidate_summaries(
    source: Evidence, publication_id: str, metrics: Sequence[str]
) -> tuple[MetricSummary, ...]:
    source.validate()
    if not source.cohort_complete:
        raise SemanticError(
            "incomplete_evidence", "Publication requires complete source cohort"
        )
    if not publication_id or not metrics or len(set(metrics)) != len(metrics):
        raise SemanticError(
            "invalid_scope", "Distinct metrics and publication ID required"
        )
    grouped: dict[tuple, list] = defaultdict(list)
    for row in source.rows:
        grouped[(row["season"], row["season_type"], row["player_id"])].append(row)
    contract = load_contract()
    if any(contract.metric(key).key != key for key in metrics):
        raise SemanticError("invalid_scope", "Canonical metric keys required")
    return tuple(
        summarize_rows(
            sorted(rows, key=lambda r: (str(r["game_date"]), r["game_id"])),
            contract.metric(key),
            publication_id,
        )
        for _, rows in sorted(grouped.items())
        for key in sorted(metrics)
    )


def candidate_manifest(
    source: Evidence,
    summaries: Sequence[MetricSummary],
    *,
    publication_id: str,
    metrics: Sequence[str],
    built_at: str,
    expires_at: str,
    relation_version: str,
    source_timestamp: str,
) -> dict[str, Any]:
    """Create an offline candidate, never a publication approval."""
    return dict(
        version=VERSION,
        publication_id=publication_id,
        policy_version=POLICY,
        contract_version=load_contract().version,
        contract_digest=digest(metric_matrix()),
        source_snapshot_id=source.snapshot_id,
        source_timestamp=source_timestamp,
        source_lineage=source.source,
        relation_version=relation_version,
        built_at=built_at,
        expires_at=expires_at,
        freshness_policy="reject_expired",
        validation_status="candidate",
        metrics=sorted(metrics),
        scopes=_scopes(source),
        summary_count=len(summaries),
        summary_digest=digest(_summary_records(summaries)),
    )


def _timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Timezone required")
    return result


@dataclass(frozen=True)
class ValidatedPublication:
    publication_id: str
    source_snapshot_id: str
    scopes: frozenset[tuple[str, str]]
    metrics: frozenset[str]

    built_at: datetime
    expires_at: datetime
    contract_digest: str
    summary_digest: str
    relation_version: str

    def supports(self, query: Query, *, now: datetime) -> bool:
        """Prospective pilot eligibility, not a routing or payload-parity approval."""
        if now.tzinfo is None or not self.built_at <= now < self.expires_at:
            return False
        if self.contract_digest != digest(metric_matrix()):
            return False
        contract = load_contract()
        try:
            query.validate(contract.metric(query.metric))
        except (SemanticError, ValueError, TypeError):
            return False
        phases = (
            ("Regular Season", "Playoffs")
            if query.season_type == "Both"
            else (query.season_type,)
        )
        return (
            season_bounds(query.season)[1] < now.date()
            and {(query.season, p) for p in phases} <= self.scopes
            and query.metric == "pts"
            and query.metric in self.metrics
            and query.operation == "summary"
            and query.aggregation in ("total", "average")
            and query.player_id is not None
            and query.window == "season_to_date"
            and all(
                getattr(query, key) is None
                for key in (
                    "n",
                    "start_date",
                    "as_of",
                    "team_abbr",
                    "opponent_abbr",
                    "home_away",
                    "seasons",
                    "excluded_player_ids",
                    "min_games",
                    "min_attempts",
                    "direction",
                )
            )
        )

    def verify_binding(
        self,
        summaries: Sequence[MetricSummary],
        *,
        relation_version: str,
        now: datetime,
    ) -> None:
        """Recheck immutable full-publication binding before future consumption."""
        if (
            now.tzinfo is None
            or not self.built_at <= now < self.expires_at
            or self.contract_digest != digest(metric_matrix())
            or relation_version != self.relation_version
            or digest(_summary_records(summaries)) != self.summary_digest
        ):
            raise SemanticError(
                "invalid_evidence", "Publication binding changed or expired"
            )


def validate_publication(
    manifest: dict[str, Any],
    summaries: Sequence[MetricSummary],
    *,
    source: Evidence,
    source_snapshot: dict[str, Any],
    expected_snapshot_id: str,
    now: datetime,
) -> ValidatedPublication:
    """Reject integrity failures; capability misses are handled only after this."""
    try:
        schema = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "contracts/ask_serving_publication.schema.json"
            ).read_text()
        )
        jsonschema.Draft202012Validator(schema).validate(manifest)
        if not expected_snapshot_id or source.snapshot_id != expected_snapshot_id:
            raise ValueError("Source version does not match trusted snapshot")
        if source_snapshot.get("cohort_complete") is not True:
            raise ValueError("Explicit complete-cohort attestation required")
        verified_source = snapshot_evidence(source_snapshot)
        if source != verified_source:
            raise ValueError("Source evidence differs from checksummed snapshot")
        source.validate()
        if not source.cohort_complete:
            raise ValueError("Incomplete cohort")
        built, expiry = (
            _timestamp(manifest["built_at"]),
            _timestamp(manifest["expires_at"]),
        )
        captured = _timestamp(source_snapshot["source_timestamp"])
        if (
            now.tzinfo is None
            or not captured <= built <= now < expiry
            or (expiry - captured).total_seconds() > MAX_SOURCE_AGE_SECONDS
            or manifest["source_timestamp"] != source_snapshot["source_timestamp"]
        ):
            raise ValueError("Publication exceeds source freshness policy")
        if not manifest["relation_version"] or not isinstance(
            manifest["relation_version"], str
        ):
            raise ValueError("Immutable relation version required")
        contract = load_contract()
        for summary in summaries:
            if not isinstance(summary, MetricSummary):
                raise ValueError("Invalid summary record type")
            summary.validate(contract.metric(summary.metric))
        if digest(_summary_records(summaries)) != manifest["summary_digest"]:
            raise ValueError("Candidate summary checksum mismatch")
        expected = candidate_summaries(
            source, manifest["publication_id"], manifest["metrics"]
        )
        # Independent source reconstruction prevents an omitted player plus a
        # recomputed count/hash from falsely certifying a complete publication.
        if _summary_records(summaries) != _summary_records(expected):
            raise ValueError("Summary membership or components disagree with source")
        expected_manifest = candidate_manifest(
            source,
            expected,
            publication_id=manifest["publication_id"],
            metrics=manifest["metrics"],
            built_at=manifest["built_at"],
            expires_at=manifest["expires_at"],
            relation_version=manifest["relation_version"],
            source_timestamp=source_snapshot["source_timestamp"],
        )
        if manifest != expected_manifest:
            raise ValueError("Manifest coverage, schema or version mismatch")
        return ValidatedPublication(
            manifest["publication_id"],
            expected_snapshot_id,
            source.covered_scopes,
            frozenset(manifest["metrics"]),
            built,
            expiry,
            manifest["contract_digest"],
            manifest["summary_digest"],
            manifest["relation_version"],
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        OSError,
        jsonschema.ValidationError,
    ) as exc:
        raise SemanticError(
            "invalid_evidence", "Serving publication failed validation"
        ) from exc
