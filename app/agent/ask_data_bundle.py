"""Read-only alignment of authorized Core and development Ask publications."""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from threading import Lock
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import jsonschema

from app.agent.claims import canonical_source
from app.agent.execution import (
    CURRENT,
    ExecutionFailure,
    acquire_timed_lock,
    checkpoint,
    optional_shadow_budget,
    record_serving_shadow,
)
from app.agent.semantic_source import capture_timestamp
from app.agent.semantics import Evidence, SemanticError, load_contract
from app.agent.serving_contract import MetricSummary
from app.agent.serving_publication import (
    MAX_SOURCE_AGE_SECONDS,
    _summary_records,
    candidate_summaries,
    digest,
)
from app.seasons import season_bounds

if TYPE_CHECKING:
    from app.agent.publication_ask_source import PublicationAskSource
    from app.agent.semantic_serving import SemanticWarehouse
    from app.agent.serving_runtime import PilotPublication
    from app.agent.serving_warehouse import RefreshingServingWarehouse

BUNDLE_SCHEMA_VERSION = "ask_data_bundle/1"
_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts/ask_data_bundle/bundle-v1.schema.json"
)


@cache
def _bundle_validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(
        json.loads(_SCHEMA_PATH.read_text()),
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        checkpoint()
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return canonical_source(value)


def _require_aware(instant: datetime) -> None:
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise SemanticError("invalid_evidence", "Bundle timestamps require a timezone")


def _timestamp(value: str) -> datetime:
    instant = datetime.fromisoformat(value)
    _require_aware(instant)
    return instant.astimezone(timezone.utc)


def validate_bundle_manifest(
    manifest: Mapping[str, Any], *, now: datetime | None = None
) -> None:
    """Validate the JSON manifest, its content ID, and freshness at read time."""
    try:
        document = (
            _json_value(manifest)
            if isinstance(manifest, MappingProxyType)
            else manifest
        )
        _bundle_validator().validate(document)
        scope = document["scope"]
        first_day = date.fromisoformat(scope["first"])
        last_day = date.fromisoformat(scope["last"])
        if first_day > last_day:
            raise ValueError("Unordered scope dates")
        if "covered_scopes" in scope:
            covered = scope["covered_scopes"]
            pairs = {(item["season"], item["phase"]) for item in covered}
            if (
                len(pairs) != len(covered)
                or {season for season, _ in pairs} != set(scope["seasons"])
                or {phase for _, phase in pairs} != set(scope["phases"])
            ):
                raise ValueError(
                    "Covered scopes differ from the declared seasons and phases"
                )
            for item in covered:
                day = date.fromisoformat(item["data_through"])
                season_start, season_end = season_bounds(item["season"])
                if not first_day <= day <= last_day:
                    raise ValueError(
                        "Covered scope data_through falls outside scope bounds"
                    )
                if not season_start <= day <= season_end:
                    raise ValueError(
                        "Covered scope data_through falls outside season bounds"
                    )
        capabilities = set(document["capabilities"])
        if "rankings" in capabilities and scope["cohort_complete"] is not True:
            raise ValueError("Rankings capability requires a complete cohort")
        if capabilities & {"detail", "rich"} and document["facts_digest"] == digest(
            None
        ):
            raise ValueError("Detail and rich capabilities require bound game facts")
        freshness = document["freshness"]
        if (
            freshness["policy"] == "dev_300s"
            and "rankings" in capabilities
            and document["facts_digest"] == digest(None)
        ):
            raise ValueError(
                "Development rankings capability requires bound game facts"
            )
        source = _timestamp(freshness["source_timestamp"])
        built = _timestamp(freshness["built_at"])
        expiry = _timestamp(freshness["expires_at"])
        if not source <= built < expiry:
            raise ValueError("Unordered freshness timestamps")
        if freshness["policy"] == "dev_300s" and expiry - source > timedelta(
            seconds=MAX_SOURCE_AGE_SECONDS
        ):
            raise ValueError("Development freshness exceeds 300 seconds")
        if now is not None:
            _require_aware(now)
            if not built <= now.astimezone(timezone.utc) < expiry:
                raise ValueError("Bundle is expired or was built after the read time")
        if (
            digest({k: v for k, v in document.items() if k != "bundle_id"})
            != document["bundle_id"]
        ):
            raise ValueError("Bundle ID differs from manifest content")
    except (ValueError, OSError, OverflowError, jsonschema.ValidationError) as exc:
        if isinstance(exc, SemanticError):
            raise
        raise SemanticError(
            "invalid_evidence", "Invalid or expired Ask bundle manifest"
        ) from exc


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        checkpoint()
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _facts_digest(evidence: Evidence | None) -> str:
    checkpoint()
    if evidence is None:
        return digest(None)
    return digest(
        _json_value(
            {
                "rows": sorted(
                    evidence.rows,
                    key=lambda row: (row["season"], row["game_id"], row["player_id"]),
                ),
                "game_results": sorted(
                    evidence.game_results, key=lambda row: row["game_id"]
                ),
                "game_participants": sorted(
                    evidence.game_participants,
                    key=lambda row: (row["game_id"], row["player_id"]),
                ),
                "position_context": evidence.position_context,
            }
        ),
        check_deadline=True,
    )


def record_bundle_shadow(
    read_candidate: Callable[[], Evidence | None], reference: Evidence
) -> dict[str, str]:
    """Compare the complete inputs to a governed fact calculation."""
    try:
        with optional_shadow_budget():
            candidate = read_candidate()
            if candidate is None:
                outcome = {"status": "ineligible"}
            elif (
                candidate.covered_scopes != reference.covered_scopes
                or candidate.data_through != reference.data_through
                or candidate.complete != reference.complete
                or candidate.cohort_complete != reference.cohort_complete
                or _facts_digest(candidate) != _facts_digest(reference)
            ):
                outcome = {"status": "mismatch", "code": "source_content_differs"}
            elif (
                candidate.source != reference.source
                or candidate.snapshot_id != reference.snapshot_id
                or candidate.model_built_at != reference.model_built_at
            ):
                outcome = {"status": "match", "code": "capture_provenance_differs"}
            else:
                outcome = {"status": "match"}
            checkpoint()
    except Exception as exc:
        outcome = {
            "status": "error",
            "code": exc.code
            if isinstance(exc, (SemanticError, ExecutionFailure))
            else "shadow_failure",
        }
    record_serving_shadow(outcome)
    return outcome


@dataclass(frozen=True)
class AskDataBundle:
    manifest: Mapping[str, Any]
    summaries: tuple[MetricSummary, ...]
    evidence: Evidence | None
    provenance: Mapping[str, Any]
    auxiliary: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, Mapping):
            raise ValueError("Bundle manifest must be a mapping")
        object.__setattr__(self, "manifest", _freeze(self.manifest))
        object.__setattr__(self, "provenance", _freeze(self.provenance))
        object.__setattr__(self, "auxiliary", _freeze(self.auxiliary))
        object.__setattr__(
            self,
            "summaries",
            tuple(
                replace(
                    summary,
                    components=_freeze(summary.components),
                    component_partials=_freeze(summary.component_partials),
                )
                for summary in self.summaries
            ),
        )
        if self.evidence is not None:
            object.__setattr__(
                self,
                "evidence",
                replace(
                    self.evidence,
                    rows=_freeze(self.evidence.rows),
                    data_through=_freeze(self.evidence.data_through),
                    position_context=_freeze(self.evidence.position_context),
                    game_results=_freeze(self.evidence.game_results),
                    game_participants=_freeze(self.evidence.game_participants),
                ),
            )
        validate_bundle_manifest(self.manifest)
        if self.manifest.get("summary_digest") != digest(
            _summary_records(self.summaries)
        ):
            raise ValueError("Bundle summary_digest differs from summaries")
        if self.manifest.get("facts_digest") != _facts_digest(self.evidence):
            raise ValueError("Bundle facts_digest differs from evidence")
        if self.evidence is not None:
            scope = self.manifest["scope"]
            if self.evidence.snapshot_id != self.manifest["source_snapshot_id"]:
                raise SemanticError(
                    "invalid_evidence",
                    "Bundle evidence snapshot_id differs from manifest",
                )
            if self.evidence.cohort_complete is not scope["cohort_complete"]:
                raise SemanticError(
                    "invalid_evidence",
                    "Bundle evidence cohort_complete differs from manifest",
                )
            if "covered_scopes" in scope:
                expected_through = {
                    (item["season"], item["phase"]): item["data_through"]
                    for item in scope["covered_scopes"]
                }
                if (
                    self.evidence.covered_scopes != frozenset(expected_through)
                    or dict(self.evidence.data_through) != expected_through
                ):
                    raise SemanticError(
                        "invalid_evidence",
                        "Bundle evidence coverage differs from manifest",
                    )
            else:
                if (
                    set(self.evidence.data_through) != self.evidence.covered_scopes
                    or {s for s, _ in self.evidence.covered_scopes}
                    != set(scope["seasons"])
                    or {p for _, p in self.evidence.covered_scopes}
                    != set(scope["phases"])
                ):
                    raise SemanticError(
                        "invalid_evidence",
                        "Bundle evidence coverage differs from manifest",
                    )
                first_day = date.fromisoformat(scope["first"])
                last_day = date.fromisoformat(scope["last"])
                for (season, _), day_text in self.evidence.data_through.items():
                    day = date.fromisoformat(day_text)
                    season_start, season_end = season_bounds(season)
                    if (
                        not first_day <= day <= last_day
                        or not season_start <= day <= season_end
                    ):
                        raise SemanticError(
                            "invalid_evidence",
                            "Bundle evidence data_through falls outside manifest bounds",
                        )

    @classmethod
    def _build(
        cls,
        *,
        publication_id: str,
        source_snapshot_id: str,
        scope: dict[str, Any],
        freshness: dict[str, str],
        capabilities: list[str],
        summaries: tuple[MetricSummary, ...],
        evidence: Evidence | None,
        provenance: dict[str, Any],
        now: datetime,
    ) -> AskDataBundle:
        manifest = dict(
            schema_version=BUNDLE_SCHEMA_VERSION,
            publication_id=publication_id,
            source_snapshot_id=source_snapshot_id,
            scope=scope,
            freshness=freshness,
            capabilities=sorted(capabilities),
            summary_digest=digest(_summary_records(summaries), check_deadline=True),
            facts_digest=_facts_digest(evidence),
        )
        manifest["bundle_id"] = digest(manifest)
        validate_bundle_manifest(manifest, now=now)
        return cls(manifest, summaries, evidence, provenance)

    @classmethod
    def from_core_publication(
        cls,
        source: PublicationAskSource,
        *,
        now: datetime,
        metrics: list[str] | None = None,
        expires_at: str | None = None,
    ) -> AskDataBundle:
        try:
            _require_aware(now)
            checkpoint()
            if source.status["freshness"] not in {"fresh", "stale_usable"}:
                raise ValueError("Core publication is unavailable or expired")
            expiry = expires_at if expires_at is not None else source.expires_at
            if expiry is None or (
                source.expires_at is not None
                and _timestamp(expiry) > _timestamp(source.expires_at)
            ):
                raise ValueError("A bound Core publication expiry is required")
            if now >= _timestamp(expiry):
                raise SemanticError("invalid_evidence", "Core Ask bundle expired")
            evidence = source.evidence
            first, last = source.first, source.last
            scope = (source.season, source.phase)
            if (
                evidence.covered_scopes != {scope}
                or evidence.data_through.get(scope) != last.isoformat()
                or any(
                    date.fromisoformat(str(row["game_date"])) < first
                    for row in evidence.rows
                )
            ):
                raise ValueError("Core status and evidence scopes differ")
            summaries = candidate_summaries(
                evidence,
                source.publication_id,
                list(load_contract().metrics) if metrics is None else metrics,
                require_complete_cohort=False,
            )
            capabilities = ["points", "metrics", "detail", "rich"]
            if evidence.cohort_complete is True:
                capabilities.append("rankings")
            built_at = (
                source.built_at if source.built_at is not None else now.isoformat()
            )
            source_timestamp = (
                source.source_timestamp
                if source.source_timestamp is not None
                else built_at
            )
            return cls._build(
                publication_id=source.publication_id,
                source_snapshot_id=evidence.snapshot_id,
                scope=dict(
                    seasons=[source.season],
                    phases=[source.phase],
                    covered_scopes=[
                        dict(
                            season=source.season,
                            phase=source.phase,
                            data_through=evidence.data_through[scope],
                        )
                    ],
                    first=first.isoformat(),
                    last=last.isoformat(),
                    cohort_complete=evidence.cohort_complete,
                ),
                freshness=dict(
                    policy="authority_backed",
                    source_timestamp=source_timestamp,
                    built_at=built_at,
                    expires_at=expiry,
                ),
                capabilities=capabilities,
                summaries=summaries,
                evidence=evidence,
                provenance=source.provenance,
                now=now,
            )
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            if isinstance(exc, SemanticError) and exc.code == "invalid_evidence":
                raise
            raise SemanticError(
                "invalid_evidence", "Invalid Core Ask publication"
            ) from exc

    @classmethod
    def from_pilot_publication(
        cls, publication: PilotPublication, *, now: datetime
    ) -> AskDataBundle:
        try:
            _require_aware(now)
            approval = publication.approval
            metadata, _ = publication.expanded_metadata(now)
            coverage = {
                (season, phase): day for season, phase, day in metadata["data_through"]
            }
            appearances = metadata["appearances"]
            if (
                coverage.keys() != approval.scopes
                or {(row["season"], row["season_type"]) for row in appearances}
                != approval.scopes
            ):
                raise ValueError("Pilot metadata and approved scopes differ")
            capabilities = []
            if "pts" in approval.metrics or publication.detail_json is not None:
                capabilities.append("points")
            if metadata["capability_version"] == "ask_serving_expansion/1":
                capabilities.append("metrics")
                if publication.detail_json is not None:
                    capabilities.append("rankings")
            elif metadata["capability_version"] != "ask_serving_points/1":
                raise ValueError("Unknown Pilot capability version")
            evidence = None
            captured = metadata.get("source_timestamp") or metadata.get("captured_at")
            if (publication.detail_json is None) != (publication.detail_digest is None):
                raise ValueError("Pilot detail binding is incomplete")
            if publication.detail_json is not None:
                evidence = publication.detail_evidence(now=now)
                if (
                    evidence is None
                    or evidence.covered_scopes != approval.scopes
                    or evidence.data_through != coverage
                ):
                    raise ValueError("Pilot detail and approved coverage differ")
                detail_captured = json.loads(publication.detail_json)[
                    "source_timestamp"
                ]
                if captured is not None and _timestamp(captured) != _timestamp(
                    detail_captured
                ):
                    raise ValueError("Wire metadata and detail source_timestamp differ")
                captured = detail_captured
                capabilities.extend(["detail", "rich"])
            elif captured is None:
                # Legacy Wire metadata without bound source_timestamp: bound by the
                # 300s development window and approved built_at.
                captured = min(
                    approval.expires_at.astimezone(timezone.utc)
                    - timedelta(seconds=MAX_SOURCE_AGE_SECONDS),
                    approval.built_at.astimezone(timezone.utc),
                ).isoformat()
            return cls._build(
                publication_id=approval.publication_id,
                source_snapshot_id=approval.source_snapshot_id,
                scope=dict(
                    seasons=sorted({season for season, _ in approval.scopes}),
                    phases=sorted({phase for _, phase in approval.scopes}),
                    covered_scopes=[
                        dict(season=season, phase=phase, data_through=day)
                        for (season, phase), day in sorted(coverage.items())
                    ],
                    first=min(row["game_date"] for row in appearances),
                    last=max(coverage.values()),
                    cohort_complete=True,
                ),
                freshness=dict(
                    policy="dev_300s",
                    source_timestamp=captured,
                    built_at=approval.built_at.isoformat(),
                    expires_at=approval.expires_at.isoformat(),
                ),
                capabilities=capabilities,
                summaries=publication.summaries,
                evidence=evidence,
                provenance=dict(
                    serving=True,
                    publication_id=approval.publication_id,
                    relation_version=approval.relation_version,
                    source_snapshot_id=approval.source_snapshot_id,
                    publication_built_at=approval.built_at.isoformat(),
                    publication_expires_at=approval.expires_at.isoformat(),
                ),
                now=now,
            )
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            if isinstance(exc, SemanticError) and exc.code == "invalid_evidence":
                raise
            raise SemanticError(
                "invalid_evidence", "Invalid Pilot Ask publication"
            ) from exc


class AskBundleWarehouse:
    """Preserve historical routing and pin a publication for each governed request."""

    def __init__(
        self,
        historical: SemanticWarehouse | RefreshingServingWarehouse,
        read_publication: Callable[[str | None], PublicationAskSource | None]
        | None = None,
        clock: Callable[[], datetime] | None = None,
        *,
        publication_mode: str = "off",
    ) -> None:
        if publication_mode not in {"off", "shadow", "serve"}:
            raise ValueError("Unsupported Ask publication mode")
        self.historical = historical
        self.read_publication = read_publication
        self._clock = clock
        self.publication_mode = publication_mode
        self._core_bundle_lock = Lock()
        self._core_bundles: OrderedDict[
            tuple[str, str, str | None, str | None, str | None], AskDataBundle
        ] = OrderedDict()

    def __getattr__(self, key: str) -> Any:
        return getattr(self.historical, key)

    @contextmanager
    def _request_state(self) -> Iterator[dict[str, Any]]:
        policy = CURRENT.get()
        if policy is None:
            yield {}
            return
        # Pilot-only access shares the underlying warehouse's request identity.
        key = (
            (AskBundleWarehouse, self.historical)
            if self.read_publication is None
            else self
        )
        acquire_timed_lock(policy.bundle_lock, "ask_bundle_wait")
        try:
            yield policy.serving_state.setdefault(key, {})
        finally:
            policy.bundle_lock.release()

    @staticmethod
    def _check_pin(publication_id: str | None, selected_id: str | None) -> None:
        if publication_id is not None and selected_id != publication_id:
            raise SemanticError(
                "publication_unavailable",
                "This conversation's publication is unavailable. Start a new analysis for the active publication.",
            )

    def _read_publication(
        self, publication_id: str | None
    ) -> PublicationAskSource | None:
        try:
            source = (
                self.read_publication(publication_id)
                if self.read_publication is not None
                else None
            )
            selected_id = source.publication_id if source is not None else None
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise SemanticError(
                "publication_unavailable",
                "The authorized publication is unavailable. Try again when its verification is restored.",
            ) from exc
        self._check_pin(publication_id, selected_id)
        return source

    def _pinned_publication(
        self, state: dict[str, Any], publication_id: str | None
    ) -> PublicationAskSource | None:
        if "publication" not in state:
            source = self._read_publication(publication_id)
            state["publication"] = source
            return source
        source = state["publication"]
        self._check_pin(
            publication_id, source.publication_id if source is not None else None
        )
        return source

    def open_publication(
        self, publication_id: str | None = None
    ) -> PublicationAskSource | None:
        with self._request_state() as state:
            source = self._pinned_publication(state, publication_id)
            if source is None or self.publication_mode == "off":
                return source
            if self.publication_mode == "shadow":
                if "shadow_compared" not in state:
                    record_bundle_shadow(
                        lambda: self._core_bundle(source, state=state).evidence,
                        source.evidence,
                    )
                    state["shadow_compared"] = True
                return source
            bundle = self._core_bundle(source, state=state)
            if bundle.evidence is None:
                raise SemanticError("invalid_evidence", "Core bundle facts are missing")
            return replace(source, evidence=bundle.evidence)

    def _core_bundle(
        self,
        source: PublicationAskSource,
        *,
        now: datetime | None = None,
        state: dict[str, Any] | None = None,
    ) -> AskDataBundle:
        authority_clock = self._clock or self._utc_now
        if state is not None and (pinned := state.get("bundle")) is not None:
            if now is not None:
                validate_bundle_manifest(pinned.manifest, now=now)
            validate_bundle_manifest(pinned.manifest, now=authority_clock())
            return pinned
        key = (
            source.publication_id,
            source.evidence.snapshot_id,
            source.expires_at,
            source.built_at,
            source.source_timestamp,
        )
        acquire_timed_lock(self._core_bundle_lock, "core_bundle_wait")
        try:
            bundle = self._core_bundles.get(key)
            if bundle is None or bundle.provenance["freshness"] != source.status.get(
                "freshness"
            ):
                bundle = AskDataBundle.from_core_publication(
                    source, now=now if now is not None else authority_clock()
                )
            if now is not None:
                validate_bundle_manifest(bundle.manifest, now=now)
            validate_bundle_manifest(bundle.manifest, now=authority_clock())
            checkpoint()
            self._core_bundles[key] = bundle
            self._core_bundles.move_to_end(key)
            # Match the production reader's bound for active and pinned releases.
            if len(self._core_bundles) > 2:
                self._core_bundles.popitem(last=False)
            if state is not None:
                state["bundle"] = bundle
            return bundle
        finally:
            self._core_bundle_lock.release()

    def recheck_publication(self, source: PublicationAskSource) -> None:
        # Recheck live authority even when request-local reads use pinned evidence.
        current = self._read_publication(source.publication_id)
        if (
            current is None
            or current.evidence.snapshot_id != source.evidence.snapshot_id
        ):
            raise SemanticError(
                "publication_unavailable",
                "The publication snapshot could not be reverified.",
            )
        if source.evidence.cohort_complete and not current.evidence.cohort_complete:
            raise SemanticError(
                "publication_unavailable",
                "The publication's league coverage could not be reverified. Try again.",
            )
        if self.publication_mode == "serve":
            self._core_bundle(current)

    def open_bundle(
        self, publication_id: str | None = None, *, now: datetime | None = None
    ) -> AskDataBundle:
        if now is not None:
            _require_aware(now)
        authority_clock: Callable[[], datetime] = (
            self._clock
            if self._clock is not None
            else (
                getattr(self.historical, "clock", None)
                if self.read_publication is None
                else None
            )
            or self._utc_now
        )

        def validate_read_freshness(manifest: Mapping[str, Any]) -> None:
            if now is None:
                validate_bundle_manifest(manifest, now=authority_clock())
                return
            validate_bundle_manifest(manifest, now=now)
            validate_bundle_manifest(manifest, now=authority_clock())

        with self._request_state() as state:
            if (bundle := state.get("bundle")) is not None:
                self._check_pin(publication_id, bundle.manifest["publication_id"])
                validate_read_freshness(bundle.manifest)
                return bundle
            if self.read_publication is not None:
                source = self._pinned_publication(state, publication_id)
                if source is None:
                    raise SemanticError(
                        "publication_unavailable",
                        "No authorized Ask publication is available.",
                    )
                bundle = self._core_bundle(
                    source,
                    now=now,
                )
            else:
                read_pilot = getattr(self.historical, "open_pilot_publication", None)
                if read_pilot is None:
                    raise SemanticError(
                        "publication_unavailable",
                        "No Ask serving publication is available.",
                    )
                try:
                    publication = read_pilot()
                except SemanticError as exc:
                    if exc.code not in {"source_unavailable", "access_denied"}:
                        raise
                    raise SemanticError(
                        "publication_unavailable",
                        "The Ask serving publication is unavailable.",
                    ) from exc
                self._check_pin(publication_id, publication.approval.publication_id)
                bundle = AskDataBundle.from_pilot_publication(
                    publication, now=now if now is not None else authority_clock()
                )
            validate_read_freshness(bundle.manifest)
            state["bundle"] = bundle
            return bundle

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.fromisoformat(capture_timestamp())
