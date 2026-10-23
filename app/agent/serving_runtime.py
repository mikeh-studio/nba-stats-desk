"""Validated, feature-gated serving and shadow evaluation; routes default off.

Full-source certification is a construction step. Consumption checks compact
publication bindings. Aggregate evidence never impersonates game-grain Evidence.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any

from app.agent.execution import (
    ExecutionFailure,
    acquire_timed_lock,
    checkpoint,
    optional_model_budget,
    record_data_read,
    record_serving_shadow,
    timed,
)
from app.agent.semantic_serving import source_players
from app.agent.semantic_source import snapshot_evidence
from app.agent.semantics import (
    PHASES,
    Contract,
    Evidence,
    Query,
    SemanticError,
    finish_summaries,
    load_contract,
    run_query,
)
from app.agent.serving_contract import (
    MetricSummary,
    aggregate_summaries,
    metric_content_digest,
)
from app.agent.serving_publication import (
    ValidatedPublication,
    candidate_manifest,
    candidate_summaries,
    digest,
    validate_publication,
)


@dataclass(frozen=True)
class PilotPublication:
    approval: ValidatedPublication
    summaries: tuple[MetricSummary, ...]
    # Bound metadata: membership dates, identity and provenance. No metric facts.
    metadata_json: str
    metadata_digest: str
    detail_json: str | None = None
    detail_digest: str | None = None
    _cohorts: Any = field(
        default_factory=OrderedDict, init=False, compare=False, repr=False
    )
    _cohort_lock: Any = field(
        default_factory=Lock, init=False, compare=False, repr=False
    )

    @classmethod
    @timed("serving_build")
    def build(
        cls,
        snapshot: dict,
        *,
        expected_snapshot_id: str,
        publication_id: str,
        relation_version: str,
        now: datetime,
        metrics: list[str] | None = None,
        include_detail: bool = False,
    ) -> PilotPublication:
        import json

        source = snapshot_evidence(snapshot)
        metrics = ["pts"] if metrics is None else metrics
        summaries = candidate_summaries(source, publication_id, metrics)
        captured = datetime.fromisoformat(snapshot["source_timestamp"])
        manifest = candidate_manifest(
            source,
            summaries,
            publication_id=publication_id,
            metrics=metrics,
            built_at=now.isoformat(),
            expires_at=(captured + timedelta(seconds=300)).isoformat(),
            relation_version=relation_version,
            source_timestamp=snapshot["source_timestamp"],
        )
        approval = validate_publication(
            manifest,
            summaries,
            source=source,
            source_snapshot=snapshot,
            expected_snapshot_id=expected_snapshot_id,
            now=now,
        )
        metadata = dict(
            source=source.source,
            model_built_at=source.model_built_at,
            data_through=[
                (s, p, d) for (s, p), d in sorted(source.data_through.items())
            ],
            players=source_players(source),
            teams=sorted(
                {
                    r[k]
                    for r in source.rows
                    for k in ("team_abbr", "opponent_abbr")
                    if r.get(k)
                }
            ),
            appearances=[
                {
                    k: r[k]
                    for k in (
                        "season",
                        "season_type",
                        "player_id",
                        "game_id",
                        "game_date",
                        "player_name",
                        "team_abbr",
                        "opponent_abbr",
                    )
                }
                for r in source.rows
            ],
        )
        ordered_seasons = list(dict.fromkeys(c["season"] for c in snapshot["coverage"]))
        metadata["sources_by_season"] = (
            dict(zip(ordered_seasons, snapshot["sources"]))
            if len(ordered_seasons) == len(snapshot["sources"])
            else {}
        )
        contract = load_contract()
        metadata["metric_source_digests"] = [
            (
                season,
                phase,
                key,
                metric_content_digest(
                    [
                        row
                        for row in source.rows
                        if (row["season"], row["season_type"]) == (season, phase)
                    ],
                    contract.metric(key),
                ),
            )
            for season, phase in sorted(source.covered_scopes)
            for key in sorted(metrics)
        ]
        metadata["capability_version"] = (
            "ask_serving_expansion/1"
            if include_detail or set(metrics) != {"pts"}
            else "ask_serving_points/1"
        )
        encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
        detail_json = (
            json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
            if include_detail
            else None
        )
        return cls(
            approval,
            summaries,
            encoded,
            digest(metadata),
            detail_json,
            digest(snapshot) if include_detail else None,
        )

    @timed("serving_validation")
    def metadata(
        self, now: datetime, query: Query
    ) -> tuple[dict[str, Any], Contract | None]:
        import json

        contract = self.approval.validate_query_binding(
            query,
            self.summaries,
            relation_version=self.approval.relation_version,
            now=now,
        )
        # The builder emits canonical JSON; hash those same bytes without a
        # redundant decode/encode pass. Changed bytes still fail before parsing.
        if (
            hashlib.sha256(self.metadata_json.encode()).hexdigest()
            != self.metadata_digest
        ):
            raise SemanticError(
                "invalid_evidence", "Serving membership binding changed"
            )
        return json.loads(self.metadata_json), contract

    @timed("serving_calculation")
    def run(
        self, query: Query, *, now: datetime, reuse_cohort: bool = False
    ) -> dict[str, Any] | None:
        checkpoint()
        # Freshness/integrity errors remain failures, not capability misses.
        metadata, contract = self.metadata(now, query)
        if contract is None:
            return None
        if reuse_cohort:
            return self._cached_summary(query, metadata, contract)
        return self._summary(query, metadata, contract)

    def _summary(self, query: Query, metadata: dict, contract: Contract) -> dict:
        metric = contract.metric(query.metric)
        phases = PHASES if query.season_type == "Both" else (query.season_type,)
        scopes = [(query.season, p) for p in phases]
        appearances = [
            r
            for r in metadata["appearances"]
            if (r["season"], r["season_type"]) in scopes
        ]
        anchor = max(date.fromisoformat(r["game_date"]) for r in appearances)
        dates = {(r["player_id"], r["game_id"]): r["game_date"] for r in appearances}
        grouped: dict[int, list[MetricSummary]] = {}
        for summary in self.summaries:
            if (
                summary.season,
                summary.phase,
            ) in scopes and summary.metric == query.metric:
                grouped.setdefault(summary.player_id, []).append(summary)
        summaries = []
        minimum = query.min_games if query.min_games is not None else contract.min_games
        for player, group in grouped.items():
            checkpoint()
            value = aggregate_summaries(group, metric, query.aggregation)
            value["game_ids"].sort(key=lambda g: (dates[(player, g)], g), reverse=True)
            reasons = []
            if value["valid_games"] < minimum:
                reasons.append("insufficient_games")
            if value["missing_component_games"]:
                reasons.append("incomplete_components")
            if value["value"] is None:
                reasons.append("unavailable_metric")
            if metric.attempt_field and query.min_attempts is not None:
                if (
                    value["components"].get(metric.attempt_field, 0)
                    < query.min_attempts
                ):
                    reasons.append("insufficient_attempts")
            summaries.append(
                dict(
                    player_id=player,
                    **value,
                    rank=None,
                    percentile=None,
                    eligible=not reasons,
                    status="unavailable"
                    if value["value"] is None
                    else "partial"
                    if value["missing_component_games"]
                    else "ok",
                    exclusion_reasons=reasons,
                    sample_warning=None,
                )
            )
        checkpoint()
        return finish_summaries(
            summaries,
            query,
            metric,
            contract,
            minimum,
            scopes,
            [query.season],
            anchor,
            None,
            anchor,
            {(s, p): d for s, p, d in metadata["data_through"]},
            scoped_source(metadata, [query.season]),
            self.approval.source_snapshot_id,
            metadata["model_built_at"],
            True,
        )

    def expanded_metadata(self, now: datetime) -> tuple[dict, Contract]:
        import json

        contract = self.approval.validate_contract_binding(
            self.summaries, relation_version=self.approval.relation_version, now=now
        )
        if (
            hashlib.sha256(self.metadata_json.encode()).hexdigest()
            != self.metadata_digest
        ):
            raise SemanticError(
                "invalid_evidence", "Serving membership binding changed"
            )
        return json.loads(self.metadata_json), contract

    def detail_evidence(self, *, now: datetime) -> Evidence | None:
        metadata, _ = self.expanded_metadata(now)
        import json

        if self.detail_json is None:
            return None
        if hashlib.sha256(self.detail_json.encode()).hexdigest() != self.detail_digest:
            raise SemanticError("invalid_evidence", "Detail checksum changed")
        snapshot = json.loads(self.detail_json)
        if snapshot.get("sha256") != self.approval.source_snapshot_id:
            raise SemanticError("invalid_evidence", "Detail source version differs")
        return snapshot_evidence(snapshot)

    @timed("serving_expanded_calculation")
    def run_expanded(
        self, query: Query, *, now: datetime, reuse_cohort: bool = False
    ) -> dict | None:
        from app.seasons import season_bounds

        metadata, contract = self.expanded_metadata(now)
        if metadata.get("capability_version") != "ask_serving_expansion/1":
            return None
        metric = contract.metric(query.metric)
        query.validate(metric)
        seasons = query.seasons or [query.season]
        if any(season_bounds(s)[1] >= now.date() for s in seasons):
            return None
        phases = PHASES if query.season_type == "Both" else (query.season_type,)
        if not {(s, p) for s in seasons for p in phases} <= self.approval.scopes:
            return None
        if query.metric not in self.approval.metrics:
            return None
        if (
            query.operation in ("rank", "percentile")
            and metric.denominator
            and query.min_attempts is None
        ):
            raise SemanticError(
                "clarification_required", "Specify an attempt threshold"
            )
        whole = query.window == "season_to_date" and all(
            getattr(query, k) is None
            for k in (
                "n",
                "start_date",
                "as_of",
                "team_abbr",
                "opponent_abbr",
                "home_away",
                "seasons",
            )
        )
        if whole and query.operation in ("summary", "rank", "percentile"):
            if reuse_cohort:
                return self._cached_summary(query, metadata, contract)
            return self._summary(query, metadata, contract)
        # Actual source-certified game rows support exact windows/logs/filters.
        # They never masquerade as aggregate rows and are never mixed versions.
        evidence = self.detail_evidence(now=now)
        if evidence is None:
            return None
        evidence = replace(evidence, source=scoped_source(metadata, seasons))
        return run_query(evidence, query, contract)

    def _cached_summary(self, query, metadata, contract):
        key = (
            query.season,
            query.season_type,
            query.metric,
            query.aggregation,
            query.min_games,
            query.min_attempts,
            query.direction,
        )
        acquire_timed_lock(self._cohort_lock, "serving_cohort_wait", 5)
        try:
            full = self._cohorts.get(key)
            hit = full is not None
            if full is None:
                whole = replace(
                    query,
                    player_id=None,
                    operation="summary",
                    excluded_player_ids=None,
                    limit=len(metadata["players"]) + 1,
                )
                full = self._summary(whole, metadata, contract)
                self._cohorts[key] = full
                while len(self._cohorts) > 32:
                    self._cohorts.popitem(last=False)
            self._cohorts.move_to_end(key)
            # Select before copying: no caller can mutate the shared cohort.
            rows = [
                r
                for r in full["rows"]
                if (
                    query.operation == "summary"
                    or (query.operation == "percentile" and query.player_id is not None)
                    or r["eligible"]
                )
                and (query.player_id is None or r["player_id"] == query.player_id)
                and r["player_id"] not in (query.excluded_player_ids or [])
            ]
            result = deepcopy({k: v for k, v in full.items() if k != "rows"})
            result["rows"] = deepcopy(rows[: query.limit])
            from dataclasses import asdict

            result["scope"].update(asdict(query))
            result["scope"]["as_of"] = full["scope"]["as_of"]
            result["status"] = (
                "ok"
                if rows
                else "no_observations"
                if query.operation == "summary"
                else "empty_cohort"
            )
            record_data_read(
                kind="evidence_cache",
                status="ok",
                rows=len(result["rows"]),
                cache_hit=hit,
                source_version=self.approval.source_snapshot_id,
            )
            return result
        finally:
            self._cohort_lock.release()


class ServingShadow:
    """Bounded synchronous shadow: reference answer remains the returned result."""

    def __init__(
        self,
        publication: PilotPublication,
        now,
        *,
        budget_seconds: float = 0.1,
        expanded: bool = False,
        query_runner=None,
        reference_evidence=None,
    ):
        if not 0 < budget_seconds <= 15:
            raise ValueError("Shadow budget must be positive and at most 15 seconds")
        self.query_runner = query_runner
        self.reference_evidence = reference_evidence
        self.expanded = expanded
        self.budget_seconds = budget_seconds
        self.outcomes_dropped = 0
        self.publication = publication
        self.clock = now
        self.outcomes: list[dict] = []

    def compare(self, query: Query, reference: dict) -> None:

        try:
            checkpoint()
            with optional_model_budget(self.budget_seconds, reserve_seconds=1.0):
                runner = (
                    self.publication.run_expanded
                    if self.expanded
                    else self.publication.run
                )
                candidate = (
                    self.query_runner(query)
                    if self.query_runner
                    else runner(query, now=self.clock())
                )
                result = self._parity(query, candidate, reference)
        except Exception as exc:
            result = dict(
                status="error",
                code=exc.code
                if isinstance(exc, (SemanticError, ExecutionFailure))
                else "shadow_failure",
            )
        record_serving_shadow(result)
        # No questions, identities or payloads in metadata. Bound memory usage.
        if len(self.outcomes) < 64:
            self.outcomes.append(result)
        else:
            self.outcomes_dropped += 1

    def _parity(self, query, candidate, reference):
        if candidate is None:
            return {"status": "ineligible"}
        if candidate == reference:
            return {"status": "match"}
        left, right = deepcopy(candidate), deepcopy(reference)
        for value in (left, right):
            for key in ("snapshot_id", "source"):
                value.get("provenance", {}).pop(key, None)
        if left != right:
            return {"status": "mismatch"}
        # Different captures may prove the same inputs. Never ignore provenance
        # merely because rounded outputs happen to agree.
        if self.reference_evidence is None:
            return {"status": "mismatch", "code": "provenance_unverified"}
        metadata, contract = self.publication.expanded_metadata(self.clock())
        expected = {(s, p, k): v for s, p, k, v in metadata["metric_source_digests"]}
        phases = PHASES if query.season_type == "Both" else (query.season_type,)
        scopes = [(s, p) for s in (query.seasons or [query.season]) for p in phases]
        for season, phase in scopes:
            rows = [
                row
                for row in self.reference_evidence.rows
                if (row["season"], row["season_type"]) == (season, phase)
            ]
            if expected.get((season, phase, query.metric)) != metric_content_digest(
                rows, contract.metric(query.metric)
            ):
                return {"status": "mismatch", "code": "source_content_differs"}
        return {"status": "match", "code": "capture_provenance_differs"}


def scoped_source(metadata, seasons):
    mapping = metadata.get("sources_by_season", {})
    return (
        ", ".join(mapping[s] for s in seasons)
        if set(seasons) <= set(mapping)
        else metadata["source"]
    )
