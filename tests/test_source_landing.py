from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dags"))

import nba_pipeline as pipeline
import nba_source_contracts as contracts
import source_landing as landing


@pytest.mark.parametrize("failure", [None, "validation", "upload"])
def test_raw_audit_precedes_validation_and_only_accepted_rows_land(
    monkeypatch, tmp_path, failure
):
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path / "airflow"))
    pytest.importorskip("airflow")
    raw = pd.DataFrame({"date": ["2026-01-01", "invalid"]})
    accepted = raw.iloc[:1].copy()
    quarantine = raw.iloc[1:].copy()
    events = []

    def snapshot(**kwargs):
        kind = kwargs["snapshot_type"]
        events.append(kind)
        pd.testing.assert_frame_equal(
            kwargs["frame"], raw if kind == "raw_extract" else quarantine
        )
        return "gs://audit/" + kind

    def validate(domain, frame):
        assert domain == "schedule"
        pd.testing.assert_frame_equal(frame, raw)
        events.append("validate")
        if failure == "validation":
            raise contracts.SourceContractError(
                {"fatal_count": 1}, quarantine_frame=quarantine
            )
        return contracts.SourceContractValidation(
            accepted, quarantine, {"status": "passed"}
        )

    def audit(**kwargs):
        assert kwargs["raw_snapshot_uri"] == "gs://audit/raw_extract"
        assert kwargs["quarantine_uri"] == "gs://audit/quarantine"
        uri = kwargs.get("landing_uri", "")
        events.append("audit_landing" if uri else "audit_validation")
        return {
            **kwargs["result"],
            "raw_snapshot_uri": kwargs["raw_snapshot_uri"],
            "quarantine_uri": kwargs["quarantine_uri"],
            "landing_uri": uri,
        }

    def path(frame):
        pd.testing.assert_frame_equal(frame, accepted)
        events.append("path")
        return "accepted/20260101.csv"

    def upload(frame, project, bucket, blob):
        pd.testing.assert_frame_equal(frame, accepted)
        assert (project, bucket, blob) == ("demo", "landing", "accepted/20260101.csv")
        events.append("upload")
        if failure == "upload":
            raise RuntimeError("upload failed")
        return "gs://landing/accepted/20260101.csv"

    monkeypatch.setattr(landing, "persist_source_extract_snapshot", snapshot)
    monkeypatch.setattr(contracts, "validate_source_contract", validate)
    monkeypatch.setattr(landing, "record_source_contract_audit", audit)
    monkeypatch.setattr(pipeline, "upload_df_to_gcs", upload)
    kwargs = dict(
        project_id="demo",
        metadata_dataset="metadata",
        location="US",
        bucket_name="landing",
        season="2025-26",
        build_blob_path=path,
    )
    before_landing = ["raw_extract", "validate", "quarantine", "audit_validation"]
    if failure == "validation":
        from airflow.exceptions import AirflowFailException

        with pytest.raises(AirflowFailException, match="1 fatal"):
            landing.land_source_frame("schedule", raw, **kwargs)
        assert events == before_landing
    elif failure == "upload":
        with pytest.raises(RuntimeError, match="upload failed"):
            landing.land_source_frame("schedule", raw, **kwargs)
        assert events == before_landing + ["path", "upload"]
    else:
        frame, contract, uri = landing.land_source_frame("schedule", raw, **kwargs)
        pd.testing.assert_frame_equal(frame, accepted)
        assert uri == contract["landing_uri"] == "gs://landing/accepted/20260101.csv"
        assert events == before_landing + ["path", "upload", "audit_landing"]
    assert len(raw) == 2


def test_raw_snapshot_failure_prevents_validation_and_landing(monkeypatch):
    monkeypatch.setattr(
        landing,
        "persist_source_extract_snapshot",
        Mock(side_effect=RuntimeError("audit storage failed")),
    )
    validate = Mock()
    upload = Mock()
    monkeypatch.setattr(landing, "validate_source_contract_frame", validate)
    monkeypatch.setattr(pipeline, "upload_df_to_gcs", upload)
    with pytest.raises(RuntimeError, match="audit storage failed"):
        landing.land_source_frame(
            "schedule",
            pd.DataFrame({"date": ["2026-01-01"]}),
            project_id="demo",
            metadata_dataset="metadata",
            location="US",
            bucket_name="landing",
            season="2025-26",
            build_blob_path=lambda frame: "unused",
        )
    validate.assert_not_called()
    upload.assert_not_called()
