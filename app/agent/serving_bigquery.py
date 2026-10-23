"""Immutable development serving relations and bounded, read-only consumption.

A trusted reference (returned by offline publication) binds the manifest digest
and source ID outside the fetched rows. The Ask reader exposes no write methods.
One atomic WRITE_EMPTY load publishes the single physical relation. Core and
optional game detail are separate reads from that same immutable table.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any

import jsonschema

from app.agent.execution import (
    acquire_timed_lock,
    bounded_timeout,
    checkpoint,
    record_data_read,
    serialized_warehouse_read,
    timed,
    warehouse_budget,
    warehouse_bytes_billed,
)
from app.agent.semantics import PHASES, Evidence, SemanticError, run_query
from app.agent.serving_contract import MetricSummary
from app.agent.serving_publication import ValidatedPublication, _summary_records, digest
from app.agent.serving_runtime import PilotPublication, scoped_source


@dataclass(frozen=True)
class ServingReference:
    project: str
    dataset: str
    table: str
    manifest_digest: str
    source_snapshot_id: str
    scopes: tuple[tuple[str, str], ...] | None = None

    def __post_init__(self):
        from app.seasons import validate_season

        if self.scopes is not None:
            try:
                scopes = tuple(tuple(item) for item in self.scopes)
                if (
                    not scopes
                    or any(len(item) != 2 or item[1] not in PHASES for item in scopes)
                    or len(set(scopes)) != len(scopes)
                ):
                    raise ValueError("Invalid declared scopes")
                for season, _ in scopes:
                    validate_season(season)
                object.__setattr__(self, "scopes", tuple(sorted(scopes)))
            except (TypeError, ValueError) as exc:
                raise SemanticError(
                    "invalid_scope", "Invalid serving reference scopes"
                ) from exc

    def covers(self, seasons):
        return self.scopes is not None and {
            (s, p) for s in seasons for p in PHASES
        } <= set(self.scopes)

    def relation(self) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,62}", self.project) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", self.dataset
        ):
            raise SemanticError("invalid_scope", "Invalid serving relation")
        if not re.fullmatch(r"ask_serving_[0-9a-f]{32}", self.table):
            raise SemanticError(
                "invalid_scope", "Relation is not an immutable serving version"
            )
        if not all(
            re.fullmatch("[0-9a-f]{64}", s)
            for s in (self.manifest_digest, self.source_snapshot_id)
        ):
            raise SemanticError(
                "invalid_scope", "Trusted serving reference hashes required"
            )
        return f"{self.project}.{self.dataset}.{self.table}"


def encode_publication(
    publication: PilotPublication, *, now: datetime, relation_id: str
):
    """Internal wire contract /3; source certification is required before encoding."""
    publication.expanded_metadata(now)
    approval = asdict(publication.approval)
    approval["scopes"] = sorted(approval["scopes"])
    approval["metrics"] = sorted(approval["metrics"])
    for key in ("built_at", "expires_at"):
        approval[key] = approval[key].isoformat()
    detail = (
        json.loads(publication.detail_json)
        if publication.detail_json is not None
        else None
    )
    header = dict(
        version="ask_serving_wire/3",
        physical_relation=relation_id,
        approval=approval,
        summary_count=len(publication.summaries),
        metric_inventory={
            metric: {
                "count": sum(s.metric == metric for s in publication.summaries),
                "digest": digest(
                    _summary_records(
                        [s for s in publication.summaries if s.metric == metric]
                    )
                ),
            }
            for metric in sorted(publication.approval.metrics)
        },
        # Certification fields come from the source-reconstructed approval.
        # This is an offline certificate, not a claim of live execution/tests.
        validation_results={
            "method": "source_reconstruction/1",
            "source_snapshot_id": publication.approval.source_snapshot_id,
            "summary_digest": publication.approval.summary_digest,
            "contract_digest": publication.approval.contract_digest,
        },
        metadata_digest=publication.metadata_digest,
        detail_digest=publication.detail_digest,
        detail_count=len(detail["rows"]) if detail else 0,
        player_details={
            str(player): {
                "count": sum(r["player_id"] == player for r in detail["rows"]),
                "digest": digest(
                    [r for r in detail["rows"] if r["player_id"] == player]
                ),
            }
            for player in sorted({r["player_id"] for r in detail["rows"]})
        }
        if detail
        else {},
        detail_header={k: v for k, v in detail.items() if k != "rows"}
        if detail
        else None,
    )
    rows: list[dict[str, Any]] = []

    def add(kind, value):
        rows.append(
            dict(
                kind=kind,
                ordinal=len(rows),
                record_json=json.dumps(value, sort_keys=True, allow_nan=False),
                player_id=value["player_id"] if kind == "detail" else None,
            )
        )

    add("manifest", header)
    add("metadata", json.loads(publication.metadata_json))
    for record in _summary_records(publication.summaries):
        add("summary", record)
    if detail:
        for row in detail["rows"]:
            add("detail", row)
    return header, rows


class DevelopmentBigQueryPublisher:
    """Explicit operator/development writer; never attached to Ask tools."""

    def __init__(self, client, *, project: str, dataset: str):
        if not dataset.endswith("_dev"):
            raise ValueError("Serving publication requires an isolated _dev dataset")
        self.client, self.project, self.dataset = client, project, dataset

    def publish(
        self, publication: PilotPublication, *, now: datetime
    ) -> ServingReference:
        from google.cloud import bigquery

        checkpoint()
        started = monotonic()
        table_name = "ask_serving_" + uuid.uuid4().hex
        header, records = encode_publication(
            publication, now=now, relation_id=table_name
        )
        reference = ServingReference(
            self.project,
            self.dataset,
            table_name,
            digest(header),
            publication.approval.source_snapshot_id,
            tuple(sorted(publication.approval.scopes)),
        )
        table = bigquery.Table(
            reference.relation(),
            schema=[
                bigquery.SchemaField("kind", "STRING", mode="REQUIRED"),
                bigquery.SchemaField("ordinal", "INTEGER", mode="REQUIRED"),
                bigquery.SchemaField("player_id", "INTEGER", mode="NULLABLE"),
                bigquery.SchemaField("record_json", "STRING", mode="REQUIRED"),
            ],
        )
        table.expires = now + timedelta(days=1)
        table.clustering_fields = ["kind"]
        self.client.create_table(table)  # unique table; never overwrite
        config = bigquery.LoadJobConfig(
            schema=table.schema, write_disposition="WRITE_EMPTY"
        )
        job = self.client.load_table_from_json(
            records,
            reference.relation(),
            job_config=config,
            timeout=bounded_timeout(30),
        )
        job.result(timeout=bounded_timeout(120))
        if job.output_rows != len(records):
            raise SemanticError(
                "invalid_evidence", "Serving publication load count mismatch"
            )
        # Only expose the reference after stored core and detail pass read-back.
        reader = BigQueryServingReader(self.client, reference)
        loaded = reader.load(
            now=now, include_detail=publication.detail_json is not None
        )
        if loaded != publication:
            # Tuple membership order is canonicalized on the wire.
            if (
                digest(_summary_records(loaded.summaries))
                != digest(_summary_records(publication.summaries))
                or loaded.metadata_json != publication.metadata_json
                or loaded.detail_json != publication.detail_json
            ):
                raise SemanticError(
                    "invalid_evidence", "Stored serving publication differs"
                )
        self.last_report = {
            "rows_loaded": len(records),
            "refresh_job_count": 1,
            "readback": list(reader.read_records),
            "duration_ms": (monotonic() - started) * 1000,
        }
        return reference


class BigQueryServingReader:
    def __init__(
        self,
        client,
        reference: ServingReference,
        *,
        max_rows: int = 100000,
        maximum_bytes_billed: int = 100000000,
    ):
        reference.relation()
        if (
            type(max_rows) is not int
            or not 1 <= max_rows <= 100000
            or type(maximum_bytes_billed) is not int
            or not 20_000_000 <= maximum_bytes_billed <= 100_000_000
        ):
            raise ValueError("Invalid serving row/byte bounds")
        self.read_records: list[dict[str, Any]] = []
        self._player_lock = Lock()
        self._players: OrderedDict[int, list[dict[str, Any]]] = OrderedDict()
        self._core_lock = Lock()
        self._detail_lock = Lock()
        self._core: PilotPublication | None = None
        self._header: dict[str, Any] | None = None
        self._detail: PilotPublication | None = None
        self.client, self.reference, self.max_rows, self.byte_budget = (
            client,
            reference,
            max_rows,
            maximum_bytes_billed,
        )

    @timed("serving_retrieval")
    @serialized_warehouse_read
    def _read(self, detail: bool, player_id: int | None = None):
        from google.api_core.exceptions import Forbidden, GoogleAPICallError
        from google.cloud import bigquery

        predicate = (
            "kind = 'detail'"
            if detail
            else "kind IN ('manifest', 'metadata', 'summary')"
        )
        if player_id is not None:
            predicate += " AND player_id = @player_id"
        sql = f"SELECT kind, ordinal, record_json, COUNT(*) OVER() AS record_count FROM `{self.reference.relation()}` WHERE {predicate} ORDER BY ordinal LIMIT @row_limit"
        parameters = [
            bigquery.ScalarQueryParameter(
                "row_limit",
                "INT64",
                (min(self.max_rows, 1000) if player_id is not None else self.max_rows)
                + 1,
            )
        ]
        if player_id is not None:
            parameters.append(
                bigquery.ScalarQueryParameter("player_id", "INT64", player_id)
            )
        config = bigquery.QueryJobConfig(
            query_parameters=parameters,
            maximum_bytes_billed=warehouse_budget(self.byte_budget // 2),
            use_query_cache=False,
            labels={"purpose": "ask-serving-read"},
        )
        job = None
        failure: Exception | None = None
        rows = None
        status = "error"
        timeout = bounded_timeout(30)
        try:
            job = self.client.query(sql, job_config=config, timeout=timeout)
            rows = [dict(r) for r in job.result(timeout=bounded_timeout(120))]
            if (
                not rows
                or len(rows) > self.max_rows
                or any(r["record_count"] != len(rows) for r in rows)
            ):
                raise SemanticError(
                    "invalid_evidence", "Empty or truncated serving relation"
                )
            status = "ok"
            return rows
        except Forbidden as exc:
            failure = exc
            raise SemanticError("access_denied", "Serving read denied") from exc
        except (GoogleAPICallError, TimeoutError) as exc:
            failure = exc
            raise SemanticError("source_unavailable", "Serving read failed") from exc
        finally:
            if status != "ok" and job is not None and hasattr(job, "cancel"):
                try:
                    job.cancel(timeout=1.0)
                except Exception:
                    pass
            if len(self.read_records) < 64:
                self.read_records.append(
                    {
                        "status": status,
                        "bytes_billed": warehouse_bytes_billed(job, failure),
                        "bytes_processed": getattr(job, "total_bytes_processed", None),
                        "rows": len(rows) if rows is not None else None,
                        "cache_hit": getattr(job, "cache_hit", None),
                    }
                )
            record_data_read(
                kind="warehouse",
                budget_ceiling=config.maximum_bytes_billed,
                transferred_bytes=sum(len(r["record_json"].encode()) for r in rows)
                if rows is not None
                else None,
                status=status,
                rows=len(rows) if rows is not None else None,
                bytes_processed=getattr(job, "total_bytes_processed", None),
                bytes_billed=warehouse_bytes_billed(job, failure),
                cache_hit=getattr(job, "cache_hit", None),
            )

    def _decode(self, *, now: datetime) -> PilotPublication:
        try:
            rows = self._read(False)
            if len({r["ordinal"] for r in rows}) != len(rows):
                raise ValueError("Duplicate wire ordinal")
            manifests = [
                json.loads(r["record_json"]) for r in rows if r["kind"] == "manifest"
            ]
            metadata = [
                json.loads(r["record_json"]) for r in rows if r["kind"] == "metadata"
            ]
            if len(manifests) != 1 or len(metadata) != 1:
                raise ValueError("Manifest and metadata required")
            header = manifests[0]
            schema = json.loads(
                (
                    Path(__file__).resolve().parents[2]
                    / "contracts/ask_serving_wire.schema.json"
                ).read_text()
            )
            jsonschema.Draft202012Validator(schema).validate(header)
            if (
                header["version"] != "ask_serving_wire/3"
                or header["physical_relation"] != self.reference.table
                or digest(header) != self.reference.manifest_digest
            ):
                raise ValueError("Untrusted manifest")
            values = dict(header["approval"])
            values["built_at"] = datetime.fromisoformat(values["built_at"])
            values["expires_at"] = datetime.fromisoformat(values["expires_at"])
            values["scopes"] = frozenset(tuple(s) for s in values["scopes"])
            values["metrics"] = frozenset(values["metrics"])
            approval = ValidatedPublication(**values)
            if self.reference.scopes is not None and approval.scopes != frozenset(
                self.reference.scopes
            ):
                raise ValueError("Trusted coverage differs from stored manifest")
            if header["validation_results"] != {
                "method": "source_reconstruction/1",
                "source_snapshot_id": approval.source_snapshot_id,
                "summary_digest": approval.summary_digest,
                "contract_digest": approval.contract_digest,
            }:
                raise ValueError("Validation certificate differs from approval")
            if approval.source_snapshot_id != self.reference.source_snapshot_id:
                raise ValueError("Untrusted source ID")
            summaries = []
            for row in rows:
                if row["kind"] == "summary":
                    value = json.loads(row["record_json"])
                    value["game_ids"] = tuple(value["game_ids"])
                    value["valid_game_ids"] = tuple(value["valid_game_ids"])
                    value["component_partials"] = {
                        k: tuple(v) for k, v in value["component_partials"].items()
                    }
                    summaries.append(MetricSummary(**value))
            if (
                len(summaries) != header["summary_count"]
                or len(rows) != len(summaries) + 2
            ):
                raise ValueError("Incomplete core inventory")
            expected_inventory = {
                metric: {
                    "count": sum(s.metric == metric for s in summaries),
                    "digest": digest(
                        _summary_records([s for s in summaries if s.metric == metric])
                    ),
                }
                for metric in sorted(approval.metrics)
            }
            if header["metric_inventory"] != expected_inventory:
                raise ValueError("Metric inventory mismatch")
            publication = PilotPublication(
                approval,
                tuple(summaries),
                json.dumps(metadata[0], sort_keys=True, separators=(",", ":")),
                header["metadata_digest"],
                None,
                header["detail_digest"],
            )
            self._header = header
            publication.expanded_metadata(now)
            return publication
        except (
            ValueError,
            KeyError,
            TypeError,
            jsonschema.ValidationError,
            OSError,
        ) as exc:
            if isinstance(exc, SemanticError):
                raise
            raise SemanticError(
                "invalid_evidence", "Invalid serving wire publication"
            ) from exc

    def load(self, *, now: datetime, include_detail: bool = False) -> PilotPublication:
        core = self._core
        if core is None:
            acquire_timed_lock(self._core_lock, "serving_reader_wait", 120)
            try:
                if self._core is None:
                    self._core = self._decode(now=now)
                core = self._core
            finally:
                self._core_lock.release()
        core.expanded_metadata(now)
        if not include_detail:
            return core
        detail = self._detail
        if detail is not None:
            detail.expanded_metadata(now)
            return detail
        acquire_timed_lock(self._detail_lock, "serving_detail_wait", 120)
        try:
            if self._detail is None:
                header = self._header
                if (
                    header is None
                    or digest(header) != self.reference.manifest_digest
                    or not header["detail_header"]
                ):
                    raise SemanticError(
                        "invalid_evidence", "Bound detail manifest required"
                    )
                records = self._read(True)
                if (
                    len(records) != header["detail_count"]
                    or len({r["ordinal"] for r in records}) != len(records)
                    or any(r["kind"] != "detail" for r in records)
                ):
                    raise SemanticError(
                        "invalid_evidence", "Incomplete detail inventory"
                    )
                snapshot = dict(
                    header["detail_header"],
                    rows=[json.loads(r["record_json"]) for r in records],
                )
                detail = replace(
                    core,
                    detail_json=json.dumps(
                        snapshot, sort_keys=True, separators=(",", ":")
                    ),
                )
                detail.detail_evidence(now=now)
                self._detail = detail
            self._detail.expanded_metadata(now)
            return self._detail
        except (
            ValueError,
            KeyError,
            TypeError,
            jsonschema.ValidationError,
            OSError,
        ) as exc:
            if isinstance(exc, SemanticError):
                raise
            raise SemanticError(
                "invalid_evidence", "Invalid serving detail publication"
            ) from exc
        finally:
            self._detail_lock.release()

    @timed("serving_bounded_games")
    def run_game_log(self, query, *, now):
        from app.seasons import season_bounds

        core = self.load(now=now)
        metadata, contract = core.expanded_metadata(now)
        query.validate(contract.metric(query.metric))
        phases = PHASES if query.season_type == "Both" else (query.season_type,)
        if (
            query.operation != "game_log"
            or query.player_id is None
            or query.seasons is not None
            or query.metric not in core.approval.metrics
            or season_bounds(query.season)[1] >= now.date()
            or not {(query.season, p) for p in phases} <= core.approval.scopes
        ):
            return None
        header = self._header
        if header is None:
            return None
        if digest(header) != self.reference.manifest_digest:
            raise SemanticError("invalid_evidence", "Serving manifest changed")
        if not header["detail_header"]:
            return None
        expected = header["player_details"].get(
            str(query.player_id), {"count": 0, "digest": digest([])}
        )
        if expected["count"] > 1000:
            return None
        acquire_timed_lock(self._player_lock, "serving_game_cache_wait", 120)
        try:
            records = self._players.get(query.player_id)
            hit = records is not None
            if records is None:
                rows = self._read(True, query.player_id) if expected["count"] else []
                if len({r["ordinal"] for r in rows}) != len(rows):
                    raise SemanticError(
                        "invalid_evidence", "Duplicate player game records"
                    )
                try:
                    records = [json.loads(r["record_json"]) for r in rows]
                except (ValueError, TypeError, KeyError) as exc:
                    raise SemanticError(
                        "invalid_evidence", "Invalid player game records"
                    ) from exc
                self._players[query.player_id] = records
                while len(self._players) > 32:
                    self._players.popitem(last=False)
            self._players.move_to_end(query.player_id)
            if (
                len(records) != expected["count"]
                or digest(records) != expected["digest"]
            ):
                raise SemanticError(
                    "invalid_evidence", "Incomplete player game membership"
                )
            record_data_read(
                kind="evidence_cache",
                status="ok",
                rows=len(records),
                cache_hit=hit,
                source_version=core.approval.source_snapshot_id,
            )
        finally:
            self._player_lock.release()
        # Complete for this source-certified player's appearances, not a league cohort.
        evidence = Evidence(
            records,
            core.approval.scopes,
            scoped_source(metadata, query.seasons or [query.season]),
            core.approval.source_snapshot_id,
            {(s, p): d for s, p, d in metadata["data_through"]},
            model_built_at=metadata["model_built_at"],
            complete=True,
            cohort_complete=False,
        )
        if query.as_of is None:
            anchor = max(
                r["game_date"]
                for r in metadata["appearances"]
                if r["season"] == query.season and r["season_type"] in phases
            )
            query = replace(query, as_of=anchor)
        return run_query(evidence, query, contract)
