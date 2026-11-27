"""Read-only alignment of authorized Core and development Ask publications."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import jsonschema

from app.agent.claims import canonical_source
from app.agent.execution import CURRENT, acquire_timed_lock
from app.agent.semantic_source import capture_timestamp
from app.agent.semantics import Evidence, SemanticError, load_contract
from app.agent.serving_contract import MetricSummary
from app.agent.serving_publication import (
    MAX_SOURCE_AGE_SECONDS,
    _summary_records,
    candidate_summaries,
    digest,
)

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
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return canonical_source(value)


def _require_aware(instant: datetime) -> None:
    if instant.utcoffset() is None:
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
        if date.fromisoformat(scope["first"]) > date.fromisoformat(scope["last"]):
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
        freshness = document["freshness"]
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
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _facts_digest(evidence: Evidence | None) -> str:
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
        )
    )


@dataclass(frozen=True)
class AskDataBundle:
    manifest: Mapping[str, Any]
    summaries: tuple[MetricSummary, ...]
    evidence: Evidence | None
    provenance: Mapping[str, Any]
    auxiliary: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
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
            summary_digest=digest(_summary_records(summaries)),
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
            if source.status["freshness"] not in {"fresh", "stale_usable"}:
                raise ValueError("Core publication is unavailable or expired")
            expiry = expires_at if expires_at is not None else source.expires_at
            if expiry is None or (
                source.expires_at is not None
                and _timestamp(expiry) > _timestamp(source.expires_at)
            ):
                raise ValueError("A bound Core publication expiry is required")
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
                    source_timestamp=source.source_timestamp
                    if source.source_timestamp is not None
                    else now.isoformat(),
                    built_at=source.built_at
                    if source.built_at is not None
                    else now.isoformat(),
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
                capabilities.extend(["metrics", "rankings"])
            elif metadata["capability_version"] != "ask_serving_points/1":
                raise ValueError("Unknown Pilot capability version")
            evidence = None
            # The v3 pilot builder sets expiry exactly 300s after capture. A
            # detail snapshot also carries the original capture timestamp.
            captured = (
                approval.expires_at.astimezone(timezone.utc)
                - timedelta(seconds=MAX_SOURCE_AGE_SECONDS)
            ).isoformat()
            if publication.detail_json is not None:
                evidence = publication.detail_evidence(now=now)
                captured = json.loads(publication.detail_json)["source_timestamp"]
                capabilities.extend(["detail", "rich"])
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
    ) -> None:
        self.historical = historical
        self.read_publication = read_publication

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
        except (ValueError, OSError, KeyError) as exc:
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
            return self._pinned_publication(state, publication_id)

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

    def open_bundle(
        self, publication_id: str | None = None, *, now: datetime | None = None
    ) -> AskDataBundle:
        if now is not None:
            _require_aware(now)
        clock: Callable[[], datetime] = (
            getattr(self.historical, "clock", self._utc_now)
            if self.read_publication is None
            else self._utc_now
        )
        with self._request_state() as state:
            if (bundle := state.get("bundle")) is not None:
                self._check_pin(publication_id, bundle.manifest["publication_id"])
                validate_bundle_manifest(
                    bundle.manifest, now=now if now is not None else clock()
                )
                return bundle
            if self.read_publication is not None:
                source = self._pinned_publication(state, publication_id)
                if source is None:
                    raise SemanticError(
                        "publication_unavailable",
                        "No authorized Ask publication is available.",
                    )
                bundle = AskDataBundle.from_core_publication(
                    source, now=now if now is not None else clock()
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
                    publication, now=now if now is not None else clock()
                )
            validate_bundle_manifest(
                bundle.manifest, now=now if now is not None else clock()
            )
            state["bundle"] = bundle
            return bundle

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.fromisoformat(capture_timestamp())
