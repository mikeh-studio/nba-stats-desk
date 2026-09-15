"""Source audit and landing adapters used by Airflow extraction tasks.

Cloud and Airflow clients are imported only at execution time. Source fetches,
empty-response handling, and landing paths remain owned by each extractor.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Callable


def _safe_storage_token(value: str) -> str:
    """Make Airflow run IDs safe for object storage paths."""
    return re.sub(r"[^A-Za-z0-9_.=-]+", "_", value).strip("_") or "unknown"


def persist_source_extract_snapshot(
    *,
    contract_name: str,
    frame,
    project_id: str,
    bucket_name: str,
    season: str,
    snapshot_type: str,
) -> str:
    """Persist pre-validation or quarantined extract rows to GCS."""
    if frame.empty:
        return ""

    import pandas as pd
    from airflow.operators.python import get_current_context

    import nba_pipeline as pipeline

    context = get_current_context()
    run_id = _safe_storage_token(context["run_id"])
    run_stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
    snapshot_id = uuid.uuid4().hex
    blob_path = (
        f"nba_data/{season}/source_audit/{snapshot_type}/"
        f"source={contract_name}/run_id={run_id}/"
        f"{run_stamp}_{snapshot_id}_{contract_name}.csv"
    )
    return pipeline.upload_df_to_gcs(
        frame,
        project_id,
        bucket_name,
        blob_path,
        if_generation_match=0,
    )


def record_source_contract_audit(
    *,
    project_id: str,
    metadata_dataset: str,
    location: str,
    result: dict,
    raw_snapshot_uri: str = "",
    quarantine_uri: str = "",
    landing_uri: str = "",
) -> dict:
    """Persist a source contract result and return the enriched result payload."""
    from airflow.operators.python import get_current_context
    from google.cloud import bigquery as bq

    import nba_pipeline as pipeline

    context = get_current_context()
    client = bq.Client(project=project_id)
    pipeline.ensure_dataset(client, f"{project_id}.{metadata_dataset}", location)
    result_table = f"{project_id}.{metadata_dataset}.source_contract_results"
    pipeline.create_source_contract_metadata_tables(client, result_table)

    enriched = dict(result)
    enriched["raw_snapshot_uri"] = raw_snapshot_uri
    enriched["quarantine_uri"] = quarantine_uri
    enriched["landing_uri"] = landing_uri
    record = pipeline.build_source_contract_result_record(
        dag_run_id=context["run_id"],
        result=enriched,
        raw_snapshot_uri=raw_snapshot_uri,
        quarantine_uri=quarantine_uri,
        landing_uri=landing_uri,
    )
    pipeline.record_source_contract_result(client, result_table, record)
    return enriched


def validate_source_contract_frame(
    contract_name: str,
    frame,
    *,
    project_id: str,
    metadata_dataset: str,
    location: str,
    bucket_name: str,
    season: str,
    raw_snapshot_uri: str,
):
    """Validate and optionally quarantine rows before GCS landing."""
    from airflow.exceptions import AirflowFailException

    import nba_source_contracts as source_contracts

    try:
        validation = source_contracts.validate_source_contract(contract_name, frame)
    except source_contracts.SourceContractError as exc:
        quarantine_uri = persist_source_extract_snapshot(
            contract_name=contract_name,
            frame=exc.quarantine_frame,
            project_id=project_id,
            bucket_name=bucket_name,
            season=season,
            snapshot_type="quarantine",
        )
        record_source_contract_audit(
            project_id=project_id,
            metadata_dataset=metadata_dataset,
            location=location,
            result=exc.result,
            raw_snapshot_uri=raw_snapshot_uri,
            quarantine_uri=quarantine_uri,
        )
        raise AirflowFailException(str(exc)) from exc

    quarantine_uri = persist_source_extract_snapshot(
        contract_name=contract_name,
        frame=validation.quarantine_frame,
        project_id=project_id,
        bucket_name=bucket_name,
        season=season,
        snapshot_type="quarantine",
    )
    source_contract = record_source_contract_audit(
        project_id=project_id,
        metadata_dataset=metadata_dataset,
        location=location,
        result=validation.result,
        raw_snapshot_uri=raw_snapshot_uri,
        quarantine_uri=quarantine_uri,
    )
    return validation.frame, source_contract


def skipped_source_contract_result(
    contract_name: str,
    reason: str,
    *,
    project_id: str,
    metadata_dataset: str,
    location: str,
) -> dict:
    """Build a no-op source contract result for empty extract paths."""
    import nba_source_contracts as source_contracts

    result = source_contracts.skipped_contract_result(contract_name, reason=reason)
    return record_source_contract_audit(
        project_id=project_id,
        metadata_dataset=metadata_dataset,
        location=location,
        result=result,
    )


def land_source_frame(
    contract_name: str,
    frame,
    *,
    project_id: str,
    metadata_dataset: str,
    location: str,
    bucket_name: str,
    season: str,
    build_blob_path: Callable[[Any], str],
):
    """Audit raw input, validate, then land accepted rows and record their URI.

    Path construction receives the validated frame so quarantined rows cannot
    influence the landing date range. Validation failures retain their audit
    record and propagate before any accepted-data landing write.
    """
    import nba_pipeline as pipeline

    raw_snapshot_uri = persist_source_extract_snapshot(
        contract_name=contract_name,
        frame=frame,
        project_id=project_id,
        bucket_name=bucket_name,
        season=season,
        snapshot_type="raw_extract",
    )
    frame, source_contract = validate_source_contract_frame(
        contract_name,
        frame,
        project_id=project_id,
        metadata_dataset=metadata_dataset,
        location=location,
        bucket_name=bucket_name,
        season=season,
        raw_snapshot_uri=raw_snapshot_uri,
    )
    gcs_uri = pipeline.upload_df_to_gcs(
        frame, project_id, bucket_name, build_blob_path(frame)
    )
    source_contract = record_source_contract_audit(
        project_id=project_id,
        metadata_dataset=metadata_dataset,
        location=location,
        result=source_contract,
        raw_snapshot_uri=source_contract.get("raw_snapshot_uri", ""),
        quarantine_uri=source_contract.get("quarantine_uri", ""),
        landing_uri=gcs_uri,
    )
    return frame, source_contract, gcs_uri
