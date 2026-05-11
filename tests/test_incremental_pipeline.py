from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dags"))

import nba_pipeline as pipeline


def test_compute_replay_start_uses_inclusive_window():
    assert (
        pipeline.compute_replay_start("2026-02-10", replay_days=3).isoformat()
        == "2026-02-08"
    )


def test_filter_incremental_game_logs_applies_replay_window_and_dedupes():
    df = pd.DataFrame(
        [
            {
                "PLAYER_ID": 1,
                "PLAYER_NAME": "A",
                "GAME_DATE": "2026-02-07",
                "MATCHUP": "LAL vs. BOS",
                "PTS": 10,
                "REB": 5,
                "AST": 2,
                "STL": 1,
                "BLK": 0,
                "WL": "W",
                "SEASON": "2025-26",
            },
            {
                "PLAYER_ID": 1,
                "PLAYER_NAME": "A",
                "GAME_DATE": "2026-02-08",
                "MATCHUP": "LAL vs. BOS",
                "PTS": 12,
                "REB": 5,
                "AST": 2,
                "STL": 1,
                "BLK": 0,
                "WL": "l",
                "SEASON": "2025-26",
            },
            {
                "PLAYER_ID": 1,
                "PLAYER_NAME": "A",
                "GAME_DATE": "2026-02-08",
                "MATCHUP": "LAL vs. BOS",
                "PTS": 12,
                "REB": 5,
                "AST": 2,
                "STL": 1,
                "BLK": 0,
                "WL": "L",
                "SEASON": "2025-26",
            },
            {
                "PLAYER_ID": 1,
                "PLAYER_NAME": "A",
                "GAME_DATE": "2026-02-10",
                "MATCHUP": "LAL @ NYK",
                "PTS": 18,
                "REB": 6,
                "AST": 4,
                "STL": 2,
                "BLK": 1,
                "WL": "W",
                "SEASON": "2025-26",
            },
        ]
    )

    result = pipeline.filter_incremental_game_logs(
        df,
        watermark_date="2026-02-10",
        replay_days=3,
        season="2025-26",
    )

    assert len(result) == 2
    assert result["GAME_DATE"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-02-10",
        "2026-02-08",
    ]
    assert result["WL"].tolist() == ["W", "L"]


def test_filter_incremental_game_logs_keeps_only_2025_26_rows():
    df = pd.DataFrame(
        [
            {
                "PLAYER_ID": 1,
                "PLAYER_NAME": "A",
                "GAME_DATE": "2026-02-08",
                "MATCHUP": "LAL vs. BOS",
                "PTS": 12,
                "REB": 5,
                "AST": 2,
                "STL": 1,
                "BLK": 0,
                "WL": "W",
                "SEASON": "2024-25",
            },
            {
                "PLAYER_ID": 2,
                "PLAYER_NAME": "B",
                "GAME_DATE": "2026-02-09",
                "MATCHUP": "NYK @ MIA",
                "PTS": 20,
                "REB": 7,
                "AST": 8,
                "STL": 1,
                "BLK": 0,
                "WL": "L",
                "SEASON": "2025-26",
            },
        ]
    )

    result = pipeline.filter_incremental_game_logs(df, season="2025-26")

    assert len(result) == 1
    assert result.iloc[0]["PLAYER_ID"] == 2
    assert result.iloc[0]["SEASON"] == "2025-26"


def test_normalize_player_name_key_strips_diacritics():
    assert pipeline.normalize_player_name_key("Luka Dončić") == "luka doncic"
    assert pipeline.normalize_player_name_key("Luka Doncic") == "luka doncic"


def test_filter_incremental_game_logs_rejects_rows_outside_2025_26_window():
    df = pd.DataFrame(
        [
            {
                "PLAYER_ID": 1,
                "PLAYER_NAME": "A",
                "GAME_DATE": "2025-06-30",
                "MATCHUP": "LAL vs. BOS",
                "PTS": 10,
                "REB": 5,
                "AST": 2,
                "STL": 1,
                "BLK": 0,
                "WL": "W",
                "SEASON": "2025-26",
            },
            {
                "PLAYER_ID": 2,
                "PLAYER_NAME": "B",
                "GAME_DATE": "2026-02-09",
                "MATCHUP": "NYK @ MIA",
                "PTS": 20,
                "REB": 7,
                "AST": 8,
                "STL": 1,
                "BLK": 0,
                "WL": "L",
                "SEASON": "2025-26",
            },
            {
                "PLAYER_ID": 3,
                "PLAYER_NAME": "C",
                "GAME_DATE": "2026-07-01",
                "MATCHUP": "BOS @ PHI",
                "PTS": 22,
                "REB": 6,
                "AST": 4,
                "STL": 1,
                "BLK": 0,
                "WL": "W",
                "SEASON": "2025-26",
            },
        ]
    )

    result = pipeline.filter_incremental_game_logs(df, season="2025-26")

    assert len(result) == 1
    assert result.iloc[0]["PLAYER_ID"] == 2


def test_build_run_metadata_record_serializes_dates():
    record = pipeline.build_run_metadata_record(
        dag_run_id="manual__2025-02-10T00:00:00+00:00",
        season="2025-26",
        status="success",
        watermark_before="2025-02-08",
        watermark_after="2025-02-10",
        rows_extracted=10,
        rows_loaded=10,
        rows_inserted=4,
        rows_updated=6,
    )

    assert record["watermark_before"] == "2025-02-08"
    assert record["watermark_after"] == "2025-02-10"
    assert record["rows_updated"] == 6


def test_build_source_contract_result_record_serializes_details():
    result = {
        "domain": "game_logs",
        "source_name": "nba_api_player_game_logs",
        "contract_version": "1",
        "status": "quarantine",
        "rows_checked": 2,
        "rows_failed": 1,
        "rows_quarantined": 1,
        "fatal_count": 0,
        "warning_count": 0,
        "quarantine_count": 1,
        "violations": [{"rule": "scoring_bounds", "failed_rows": 1}],
    }

    record = pipeline.build_source_contract_result_record(
        dag_run_id="manual__2026-01-10T00:00:00+00:00",
        result=result,
        raw_snapshot_uri="gs://bucket/raw.csv",
        quarantine_uri="gs://bucket/quarantine.csv",
        landing_uri="gs://bucket/landing.csv",
        validated_at_utc="2026-01-10T00:00:00Z",
    )

    assert record["status"] == "quarantine"
    assert record["rows_quarantined"] == 1
    assert record["raw_snapshot_uri"] == "gs://bucket/raw.csv"
    details = json.loads(record["details_json"])
    assert details["violations"][0]["rule"] == "scoring_bounds"
    assert details["landing_uri"] == "gs://bucket/landing.csv"


def test_record_source_contract_result_uses_idempotent_merge():
    class DummyJob:
        def result(self):
            return None

    class DummyClient:
        def __init__(self):
            self.statements = []
            self.job_configs = []

        def query(self, statement, job_config=None):
            self.statements.append(statement)
            self.job_configs.append(job_config)
            return DummyJob()

    client = DummyClient()
    record = pipeline.build_source_contract_result_record(
        dag_run_id="manual__2026-01-10T00:00:00+00:00",
        result={
            "domain": "schedule",
            "source_name": "nba_api_schedule",
            "contract_version": "1",
            "status": "passed",
        },
    )

    pipeline.record_source_contract_result(
        client,
        "project.dataset.source_contract_results",
        record,
    )

    assert "MERGE `project.dataset.source_contract_results`" in client.statements[0]
    assert "WHEN MATCHED THEN UPDATE SET" in client.statements[0]
    parameter_names = {
        parameter.name
        for parameter in client.job_configs[0].query_parameters
    }
    assert {"dag_run_id", "domain", "status", "details_json"} <= parameter_names


def test_upload_df_to_gcs_can_use_create_only_precondition(monkeypatch):
    class DummyBlob:
        def __init__(self):
            self.upload_kwargs = None

        def upload_from_string(self, data, content_type=None, **kwargs):
            self.upload_kwargs = {
                "data": data,
                "content_type": content_type,
                **kwargs,
            }

    class DummyBucket:
        def __init__(self):
            self.created_blob = DummyBlob()

        def blob(self, name):
            self.blob_name = name
            return self.created_blob

    class DummyStorageClient:
        def __init__(self, project=None):
            self.project = project
            self.created_bucket = DummyBucket()

        def bucket(self, name):
            self.bucket_name = name
            return self.created_bucket

    created_clients = []

    def fake_storage_client(project=None):
        client = DummyStorageClient(project=project)
        created_clients.append(client)
        return client

    monkeypatch.setattr(pipeline.storage, "Client", fake_storage_client)

    uri = pipeline.upload_df_to_gcs(
        pd.DataFrame([{"a": 1}]),
        "project-id",
        "bucket-name",
        "path/file.csv",
        if_generation_match=0,
    )

    blob = created_clients[0].created_bucket.created_blob
    assert uri == "gs://bucket-name/path/file.csv"
    assert blob.upload_kwargs["content_type"] == "text/csv"
    assert blob.upload_kwargs["if_generation_match"] == 0


def test_upsert_ingestion_state_preserves_existing_newer_watermark():
    class DummyJob:
        def result(self):
            return None

    class DummyClient:
        def __init__(self):
            self.statements = []

        def query(self, statement, job_config=None):
            self.statements.append(statement)
            return DummyJob()

    client = DummyClient()

    pipeline.upsert_ingestion_state(
        client,
        "project.dataset.ingestion_state",
        season="2025-26",
        watermark_date="2026-05-01",
    )

    assert (
        "WHEN T.watermark_date IS NULL OR S.watermark_date > T.watermark_date"
        in client.statements[0]
    )
    assert "ELSE T.watermark_date" in client.statements[0]


def test_validate_merge_reconciliation_returns_unchanged_rows():
    result = pipeline.validate_merge_reconciliation(
        domain="game_logs",
        rows_loaded=10,
        pre_count=100,
        post_count=104,
        inserted=4,
        updated=3,
    )

    assert result["rows_loaded"] == 10
    assert result["pre_count"] == 100
    assert result["post_count"] == 104
    assert result["inserted"] == 4
    assert result["updated"] == 3
    assert result["unchanged"] == 3


def test_validate_merge_reconciliation_rejects_insert_update_overflow():
    try:
        pipeline.validate_merge_reconciliation(
            domain="schedule",
            rows_loaded=5,
            pre_count=20,
            post_count=23,
            inserted=3,
            updated=3,
        )
    except ValueError as exc:
        assert "inserted+updated" in str(exc)
    else:
        raise AssertionError("Expected reconciliation overflow to raise ValueError")


def test_validate_merge_reconciliation_rejects_post_count_mismatch():
    try:
        pipeline.validate_merge_reconciliation(
            domain="game_logs",
            rows_loaded=4,
            pre_count=10,
            post_count=15,
            inserted=3,
            updated=1,
        )
    except ValueError as exc:
        assert "expected post_count 13" in str(exc)
    else:
        raise AssertionError("Expected post_count mismatch to raise ValueError")


def test_game_logs_schema_includes_game_id():
    schema_names = [field.name for field in pipeline.get_game_logs_schema()]
    assert schema_names[0] == "GAME_ID"


def test_quote_bigquery_table_id_rejects_unsafe_identifier():
    assert (
        pipeline.quote_bigquery_table_id("demo-project.nba_bronze.raw_reports")
        == "`demo-project.nba_bronze.raw_reports`"
    )
    try:
        pipeline.quote_bigquery_table_id("demo.nba_bronze.raw_reports`; DROP TABLE x;")
    except ValueError as exc:
        assert "Unsafe BigQuery table identifier" in str(exc)
    else:
        raise AssertionError("Expected unsafe table identifier to raise")


def test_injury_report_schema_includes_report_timestamp():
    schema_names = [field.name for field in pipeline.get_injury_report_schema()]
    assert "REPORT_TIMESTAMP_UTC" in schema_names
    assert "PLAYER_NAME_SOURCE" in schema_names


def test_build_injury_report_candidates_applies_max_report_cap():
    candidates = pipeline.build_injury_report_candidates(
        start_date="2026-05-01",
        end_date="2026-05-05",
        report_times_et=["5:00 PM"],
        max_reports=3,
    )

    assert [candidate["report_date"].isoformat() for candidate in candidates] == [
        "2026-05-03",
        "2026-05-04",
        "2026-05-05",
    ]
    assert candidates[-1]["source_url"].endswith("Injury-Report_2026-05-05_05_00PM.pdf")


def test_parse_injury_report_text_extracts_rows_and_reason_continuations():
    text = """
    Injury Report: 05/06/26 05:00 PM
    Page 1 of 1
    Game Date Game Time Matchup Team Player Name Current Status Reason
    05/06/2026 07:00 (ET) PHI@NYK Philadelphia 76ers Embiid, Joel Out
    Injury/Illness - Right Ankle; Sprain;
    Right Hip Soreness
    Maxey, Tyrese Available
    Injury/Illness - Right Finger; Tendon Strain - Splint
    New York Knicks Robinson, Mitchell Questionable Injury/Illness - Illness; Illness
    05/07/2026 09:30 (ET) LAL@OKC Los Angeles Lakers NOT YET SUBMITTED
    Oklahoma City Thunder Holmgren, Chet Probable Injury/Illness - Hip; Soreness
    """
    lookup = {
        "joel embiid": 203954,
        "tyrese maxey": 1630178,
        "mitchell robinson": 1629011,
        "chet holmgren": 1631096,
    }

    result = pipeline.parse_injury_report_text(
        text,
        report_date="2026-05-06",
        report_time_et="5:00 PM",
        source_url="https://example.test/injury.pdf",
        player_lookup=lookup,
        ingested_at_utc="2026-05-06T22:00:00+00:00",
    )

    assert result["PLAYER_NAME"].tolist() == [
        "Joel Embiid",
        "Tyrese Maxey",
        "Mitchell Robinson",
        "Chet Holmgren",
    ]
    embiid = result[result["PLAYER_ID"] == 203954].iloc[0]
    assert embiid["TEAM_ABBR"] == "PHI"
    assert embiid["INJURY_STATUS"] == "Out"
    assert "Right Hip Soreness" in embiid["REASON"]
    assert "NOT YET SUBMITTED" not in result["PLAYER_NAME_SOURCE"].tolist()


def test_parse_injury_report_text_handles_tokenized_pdf_extraction():
    text = """
    Injury
    Report:
    05/09/26
    05:00
    PM
    Page
    1
    of
    1
    Game
    Date
    Game
    Time
    Matchup
    Team
    Player
    Name
    Current
    Status
    Reason
    05/09/2026
    03:00
    (ET)
    DET@CLE
    Detroit
    Pistons
    Huerter,
    Kevin
    Out
    Injury/Illness
    -
    Left
    Adductor;
    Strain
    Cleveland
    Cavaliers
    Merrill,
    Sam
    Available
    Injury/Illness
    -
    Left
    Hamstring;
    Strain
    08:30
    (ET)
    OKC@LAL
    Oklahoma
    City
    Thunder
    Sorber,
    Thomas
    Out
    Injury/Illness
    -
    Right
    ACL;
    Surgical
    Recovery
    Williams,
    Jalen
    Out
    Injury/Illness
    -
    Left
    Hamstring;
    Strain
    Los
    Angeles
    Lakers
    Doncic,
    Luka
    Out
    Injury/Illness
    -
    Left
    Hamstring;
    Strain
    Vanderbilt,
    Jarred
    Questionable
    Injury/Illness
    -
    Right
    Finger;
    Dislocation
    """
    lookup = {
        "kevin huerter": 1628989,
        "sam merrill": 1630241,
        "thomas sorber": 1641767,
        "jalen williams": 1631114,
        "luka doncic": 1629029,
        "jarred vanderbilt": 1629020,
    }

    result = pipeline.parse_injury_report_text(
        text,
        report_date="2026-05-09",
        report_time_et="05_00PM",
        source_url="https://example.test/injury.pdf",
        player_lookup=lookup,
        ingested_at_utc="2026-05-09T21:00:00+00:00",
    )

    assert result["PLAYER_NAME"].tolist() == [
        "Kevin Huerter",
        "Sam Merrill",
        "Thomas Sorber",
        "Jalen Williams",
        "Luka Doncic",
        "Jarred Vanderbilt",
    ]
    assert result["TEAM_ABBR"].tolist() == ["DET", "CLE", "OKC", "OKC", "LAL", "LAL"]
    assert result["INJURY_STATUS"].tolist()[-1] == "Questionable"
    assert "Surgical Recovery" in result.iloc[2]["REASON"]


def test_parse_injury_report_text_serializes_player_id_as_nullable_integer():
    text = """
    Injury Report: 05/06/26 05:00 PM
    Game Date Game Time Matchup Team Player Name Current Status Reason
    05/06/2026 07:00 (ET) PHI@NYK Philadelphia 76ers Embiid, Joel Out Injury/Illness - Right Ankle; Sprain
    New York Knicks Robinson, Mitchell Questionable Injury/Illness - Illness; Illness
    """

    result = pipeline.parse_injury_report_text(
        text,
        report_date="2026-05-06",
        report_time_et="5:00 PM",
        source_url="https://example.test/injury.pdf",
        player_lookup={"joel embiid": 203954},
        ingested_at_utc="2026-05-06T22:00:00+00:00",
    )

    csv_output = result.to_csv(index=False)

    assert str(result["PLAYER_ID"].dtype) == "Int64"
    assert "203954.0" not in csv_output
    assert "203954" in csv_output
    assert pd.isna(result.iloc[1]["PLAYER_ID"])


def test_fetch_official_injury_report_pdf_passes_timeout_to_client():
    captured = {}

    class FakeResponse:
        status_code = 200
        content = b"%PDF-1.4"

        def raise_for_status(self):
            return None

    class FakeClient:
        def get(self, url, timeout, headers):
            captured["url"] = url
            captured["timeout"] = timeout
            captured["headers"] = headers
            return FakeResponse()

    content = pipeline.fetch_official_injury_report_pdf(
        "https://example.test/report.pdf",
        timeout=4.5,
        retries=1,
        client=FakeClient(),
    )

    assert content == b"%PDF-1.4"
    assert captured["url"] == "https://example.test/report.pdf"
    assert captured["timeout"] == 4.5
    assert captured["headers"]["Accept"].startswith("application/pdf")
    assert "Mozilla/5.0" in captured["headers"]["User-Agent"]


def test_fetch_official_injury_report_pdf_soft_fails_after_retries(monkeypatch):
    calls = []
    sleeps = []

    class FailingClient:
        def get(self, url, timeout, headers):
            calls.append((url, timeout))
            raise TimeoutError("injury report timeout")

    monkeypatch.setattr(pipeline.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = pipeline.fetch_official_injury_report_pdf(
        "https://example.test/report.pdf",
        timeout=2,
        retries=2,
        retry_base_delay=0,
        retry_max_delay=0,
        client=FailingClient(),
    )

    assert result is None
    assert calls == [
        ("https://example.test/report.pdf", 2),
        ("https://example.test/report.pdf", 2),
    ]
    assert sleeps == [0]


def test_ensure_table_has_columns_adds_expected_columns():
    class DummyJob:
        def result(self):
            return None

    class DummyClient:
        def __init__(self):
            self.statements = []

        def query(self, statement):
            self.statements.append(statement)
            return DummyJob()

    client = DummyClient()

    pipeline.ensure_table_has_columns(
        client,
        "project.dataset.raw_game_logs",
        [
            pipeline.bigquery.SchemaField("GAME_ID", "STRING"),
            pipeline.bigquery.SchemaField("FGM", "FLOAT"),
            pipeline.bigquery.SchemaField("PLAYER_ID", "INTEGER"),
            pipeline.bigquery.SchemaField("ROSTER_STATUS", "BOOLEAN"),
        ],
    )

    assert client.statements == [
        "ALTER TABLE `project.dataset.raw_game_logs` ADD COLUMN IF NOT EXISTS game_id STRING",
        "ALTER TABLE `project.dataset.raw_game_logs` ADD COLUMN IF NOT EXISTS fgm FLOAT64",
        "ALTER TABLE `project.dataset.raw_game_logs` ADD COLUMN IF NOT EXISTS player_id INT64",
        "ALTER TABLE `project.dataset.raw_game_logs` ADD COLUMN IF NOT EXISTS roster_status BOOL",
    ]


def test_calculate_nba_api_retry_delay_uses_bounded_backoff():
    assert (
        pipeline.calculate_nba_api_retry_delay(
            1,
            base_delay=1.0,
            backoff_multiplier=2.0,
            max_delay=8.0,
        )
        == 1.0
    )
    assert (
        pipeline.calculate_nba_api_retry_delay(
            3,
            base_delay=1.0,
            backoff_multiplier=2.0,
            max_delay=8.0,
        )
        == 4.0
    )
    assert (
        pipeline.calculate_nba_api_retry_delay(
            5,
            base_delay=1.0,
            backoff_multiplier=2.0,
            max_delay=8.0,
        )
        == 8.0
    )


def _player_game_log_api_frame():
    return pd.DataFrame(
        [
            {
                "Game_ID": "0022500001",
                "GAME_DATE": "2026-02-10",
                "MATCHUP": "BOS vs. NYK",
                "WL": "W",
                "MIN": 30,
                "FGM": 10,
                "FGA": 20,
                "FG_PCT": 0.5,
                "FG3M": 2,
                "FG3A": 6,
                "FG3_PCT": 0.333,
                "FTM": 4,
                "FTA": 5,
                "FT_PCT": 0.8,
                "OREB": 1,
                "DREB": 4,
                "PTS": 26,
                "REB": 5,
                "AST": 3,
                "STL": 1,
                "BLK": 0,
                "TOV": 2,
                "PF": 1,
                "PLUS_MINUS": 7,
            }
        ]
    )


def test_get_player_game_log_passes_timeout_to_nba_api(monkeypatch):
    captured = {}

    class FakePlayerGameLog:
        def __init__(self, *, player_id, season, timeout):
            captured["player_id"] = player_id
            captured["season"] = season
            captured["timeout"] = timeout

        def get_data_frames(self):
            return [_player_game_log_api_frame()]

    monkeypatch.setattr(pipeline.playergamelog, "PlayerGameLog", FakePlayerGameLog)

    result = pipeline.get_player_game_log(7, timeout=4.5, retries=1)

    assert captured == {"player_id": 7, "season": "2025-26", "timeout": 4.5}
    assert len(result) == 1
    assert result.iloc[0]["GAME_ID"] == "0022500001"


def _line_score_api_frame():
    return pd.DataFrame(
        [
            {
                "GAME_DATE_EST": "2026-02-10",
                "GAME_ID": "0022500001",
                "TEAM_ID": 1610612738,
                "TEAM_ABBREVIATION": "BOS",
                "TEAM_CITY_NAME": "Boston",
                "TEAM_NICKNAME": "Celtics",
                "TEAM_WINS_LOSSES": "40-10",
                "PTS_QTR1": 30,
                "PTS_QTR2": 25,
                "PTS_QTR3": 20,
                "PTS_QTR4": 35,
                "PTS_OT1": 0,
                "PTS_OT2": 0,
                "PTS_OT3": 0,
                "PTS_OT4": 0,
                "PTS_OT5": 0,
                "PTS_OT6": 0,
                "PTS_OT7": 0,
                "PTS_OT8": 0,
                "PTS_OT9": 0,
                "PTS_OT10": 0,
                "PTS": 110,
            }
        ]
    )


def test_get_game_line_scores_passes_timeout_to_nba_api(monkeypatch):
    captured = {}

    class FakeBoxScoreSummary:
        def __init__(self, *, game_id, timeout):
            captured["game_id"] = game_id
            captured["timeout"] = timeout

        def get_available_data(self):
            return ["LineScore"]

        def get_data_frames(self):
            return [_line_score_api_frame()]

    monkeypatch.setattr(
        pipeline.boxscoresummaryv2, "BoxScoreSummaryV2", FakeBoxScoreSummary
    )

    result = pipeline.get_game_line_scores("0022500001", timeout=5, retries=1)

    assert captured == {"game_id": "0022500001", "timeout": 5}
    assert len(result) == 1
    assert result.iloc[0]["TEAM_ABBR"] == "BOS"


def test_get_game_line_scores_accepts_dict_keys_available_data(monkeypatch):
    class FakeBoxScoreSummary:
        def __init__(self, *, game_id, timeout):
            pass

        def get_available_data(self):
            return {"LineScore": None}.keys()

        def get_data_frames(self):
            return [_line_score_api_frame()]

    monkeypatch.setattr(
        pipeline.boxscoresummaryv2, "BoxScoreSummaryV2", FakeBoxScoreSummary
    )

    result = pipeline.get_game_line_scores("0022500001", retries=1)

    assert len(result) == 1
    assert result.iloc[0]["TEAM_ABBR"] == "BOS"


def test_get_game_line_scores_soft_fails_after_retries(monkeypatch):
    calls = []
    sleeps = []

    class FailingBoxScoreSummary:
        def __init__(self, *, game_id, timeout):
            calls.append((game_id, timeout))
            raise TimeoutError("line score timeout")

    monkeypatch.setattr(
        pipeline.boxscoresummaryv2, "BoxScoreSummaryV2", FailingBoxScoreSummary
    )
    monkeypatch.setattr(pipeline.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = pipeline.get_game_line_scores(
        "0022500001",
        timeout=2,
        retries=2,
        retry_base_delay=0,
        retry_max_delay=0,
    )

    assert result.empty
    assert calls == [("0022500001", 2), ("0022500001", 2)]
    assert sleeps == [0]


def _player_reference_api_frame():
    return pd.DataFrame(
        [
            {
                "PERSON_ID": 7,
                "FIRST_NAME": "Test",
                "LAST_NAME": "Player",
                "DISPLAY_FIRST_LAST": "Test Player",
                "PLAYER_SLUG": "test-player",
                "BIRTHDATE": "2000-01-01",
                "SCHOOL": "Example U",
                "COUNTRY": "USA",
                "LAST_AFFILIATION": "Example U",
                "HEIGHT": "6-5",
                "WEIGHT": 210,
                "SEASON_EXP": 2,
                "JERSEY": "7",
                "POSITION": "G",
                "ROSTERSTATUS": 1,
                "TEAM_ID": 1610612738,
                "TEAM_NAME": "Celtics",
                "TEAM_ABBREVIATION": "BOS",
                "TEAM_CODE": "celtics",
                "TEAM_CITY": "Boston",
                "FROM_YEAR": 2023,
                "TO_YEAR": 2026,
                "DRAFT_YEAR": "2023",
                "DRAFT_ROUND": "1",
                "DRAFT_NUMBER": "7",
            }
        ]
    )


def test_get_player_reference_passes_timeout_to_nba_api(monkeypatch):
    captured = {}

    class FakeCommonPlayerInfo:
        def __init__(self, *, player_id, timeout):
            captured["player_id"] = player_id
            captured["timeout"] = timeout

        def get_available_data(self):
            return ["CommonPlayerInfo"]

        def get_data_frames(self):
            return [_player_reference_api_frame()]

    monkeypatch.setattr(
        pipeline.commonplayerinfo, "CommonPlayerInfo", FakeCommonPlayerInfo
    )

    result = pipeline.get_player_reference(7, timeout=6, retries=1)

    assert captured == {"player_id": 7, "timeout": 6}
    assert len(result) == 1
    assert result.iloc[0]["PLAYER_NAME"] == "Test Player"


def test_get_player_reference_accepts_dict_keys_available_data(monkeypatch):
    class FakeCommonPlayerInfo:
        def __init__(self, *, player_id, timeout):
            pass

        def get_available_data(self):
            return {"CommonPlayerInfo": None}.keys()

        def get_data_frames(self):
            return [_player_reference_api_frame()]

    monkeypatch.setattr(
        pipeline.commonplayerinfo, "CommonPlayerInfo", FakeCommonPlayerInfo
    )

    result = pipeline.get_player_reference(7, retries=1)

    assert len(result) == 1
    assert result.iloc[0]["PLAYER_NAME"] == "Test Player"


def test_get_player_reference_soft_fails_after_retries(monkeypatch):
    calls = []

    class FailingCommonPlayerInfo:
        def __init__(self, *, player_id, timeout):
            calls.append((player_id, timeout))
            raise TimeoutError("player reference timeout")

    monkeypatch.setattr(
        pipeline.commonplayerinfo, "CommonPlayerInfo", FailingCommonPlayerInfo
    )
    monkeypatch.setattr(pipeline.time, "sleep", lambda _seconds: None)

    result = pipeline.get_player_reference(
        7,
        timeout=2,
        retries=2,
        retry_base_delay=0,
        retry_max_delay=0,
    )

    assert result.empty
    assert calls == [(7, 2), (7, 2)]


def test_get_upcoming_schedule_soft_fails_after_retries(monkeypatch):
    calls = []
    sleeps = []

    class FailingSchedule:
        def __init__(self, *, season, timeout):
            calls.append((season, timeout))
            raise TimeoutError("schedule timeout")

    monkeypatch.setattr(pipeline.scheduleleaguev2, "ScheduleLeagueV2", FailingSchedule)
    monkeypatch.setattr(pipeline.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = pipeline.get_upcoming_schedule(
        timeout=3,
        retries=2,
        retry_base_delay=0,
        retry_max_delay=0,
    )

    assert result.empty
    assert result.columns.tolist() == [
        field.name for field in pipeline.get_schedule_schema()
    ]
    assert calls == [("2025-26", 3), ("2025-26", 3)]
    assert sleeps == [0]


def test_get_all_player_game_logs_still_raises_when_all_players_fail(monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "get_player_game_log",
        lambda *_args, **_kwargs: pd.DataFrame(),
    )

    try:
        pipeline.get_all_player_game_logs(
            [{"id": 7, "full_name": "Test Player"}],
            delay=0,
            retry_base_delay=0,
            retry_max_delay=0,
        )
    except RuntimeError as exc:
        assert "No game logs were fetched" in str(exc)
    else:
        raise AssertionError("Expected all-empty game-log extraction to raise")


def test_get_all_game_line_scores_dedupes_by_game_and_team(monkeypatch):
    def fake_get_game_line_scores(game_id: str, *, season: str = "2025-26", **_kwargs):
        return pd.DataFrame(
            [
                {
                    "GAME_DATE": "2026-02-10",
                    "GAME_ID": game_id,
                    "SEASON": season,
                    "TEAM_ID": 1,
                    "TEAM_ABBR": "BOS",
                    "TEAM_CITY_NAME": "Boston",
                    "TEAM_NICKNAME": "Celtics",
                    "TEAM_WINS_LOSSES": "40-10",
                    "PTS_QTR1": 30,
                    "PTS_QTR2": 25,
                    "PTS_QTR3": 20,
                    "PTS_QTR4": 35,
                    "PTS_OT1": 0,
                    "PTS_OT2": 0,
                    "PTS_OT3": 0,
                    "PTS_OT4": 0,
                    "PTS_OT5": 0,
                    "PTS_OT6": 0,
                    "PTS_OT7": 0,
                    "PTS_OT8": 0,
                    "PTS_OT9": 0,
                    "PTS_OT10": 0,
                    "PTS": 110,
                    "INGESTED_AT_UTC": "2026-02-10T12:00:00+00:00",
                },
                {
                    "GAME_DATE": "2026-02-10",
                    "GAME_ID": game_id,
                    "SEASON": season,
                    "TEAM_ID": 1,
                    "TEAM_ABBR": "BOS",
                    "TEAM_CITY_NAME": "Boston",
                    "TEAM_NICKNAME": "Celtics",
                    "TEAM_WINS_LOSSES": "40-10",
                    "PTS_QTR1": 30,
                    "PTS_QTR2": 25,
                    "PTS_QTR3": 20,
                    "PTS_QTR4": 35,
                    "PTS_OT1": 0,
                    "PTS_OT2": 0,
                    "PTS_OT3": 0,
                    "PTS_OT4": 0,
                    "PTS_OT5": 0,
                    "PTS_OT6": 0,
                    "PTS_OT7": 0,
                    "PTS_OT8": 0,
                    "PTS_OT9": 0,
                    "PTS_OT10": 0,
                    "PTS": 110,
                    "INGESTED_AT_UTC": "2026-02-10T12:00:00+00:00",
                },
            ]
        )

    monkeypatch.setattr(pipeline, "get_game_line_scores", fake_get_game_line_scores)
    result = pipeline.get_all_game_line_scores(
        ["001", "001"], season="2025-26", delay=0
    )

    assert len(result) == 1
    assert result.iloc[0]["GAME_ID"] == "001"
    assert result.iloc[0]["TEAM_ID"] == 1


def test_get_all_player_references_dedupes_by_player_id(monkeypatch):
    def fake_get_player_reference(player_id: int, **_kwargs):
        return pd.DataFrame(
            [
                {
                    "PLAYER_ID": player_id,
                    "FIRST_NAME": "Test",
                    "LAST_NAME": "Player",
                    "PLAYER_NAME": "Test Player",
                    "PLAYER_SLUG": "test-player",
                    "BIRTHDATE": "2000-01-01",
                    "SCHOOL": "Example U",
                    "COUNTRY": "USA",
                    "LAST_AFFILIATION": "Example U",
                    "HEIGHT": "6-5",
                    "WEIGHT": 210,
                    "SEASON_EXP": 2,
                    "JERSEY": "7",
                    "POSITION": "G",
                    "ROSTER_STATUS": True,
                    "TEAM_ID": 1610612738,
                    "TEAM_NAME": "Celtics",
                    "TEAM_ABBR": "BOS",
                    "TEAM_CODE": "celtics",
                    "TEAM_CITY": "Boston",
                    "FROM_YEAR": 2023,
                    "TO_YEAR": 2026,
                    "DRAFT_YEAR": "2023",
                    "DRAFT_ROUND": "1",
                    "DRAFT_NUMBER": "7",
                    "INGESTED_AT_UTC": "2026-02-10T12:00:00+00:00",
                }
            ]
        )

    monkeypatch.setattr(pipeline, "get_player_reference", fake_get_player_reference)
    players = [
        {"id": 7, "full_name": "Test Player"},
        {"id": 7, "full_name": "Test Player"},
    ]
    result = pipeline.get_all_player_references(players, delay=0)

    assert len(result) == 1
    assert result.iloc[0]["PLAYER_ID"] == 7


def _bootstrap_game_logs_sample():
    return pd.DataFrame(
        [
            {
                "GAME_DATE": "2026-02-10",
                "GAME_ID": "0022500001",
                "MATCHUP": "BOS vs. NYK",
                "PTS": 30,
                "SEASON": "2025-26",
                "PLAYER_ID": 1,
                "PLAYER_NAME": "Test Scorer",
                "INGESTED_AT_UTC": "2026-02-10T12:00:00+00:00",
            },
            {
                "GAME_DATE": "2026-02-10",
                "GAME_ID": "0022500001",
                "MATCHUP": "BOS vs. NYK",
                "PTS": 20,
                "SEASON": "2025-26",
                "PLAYER_ID": 2,
                "PLAYER_NAME": "Second Celtic",
                "INGESTED_AT_UTC": "2026-02-10T12:05:00+00:00",
            },
            {
                "GAME_DATE": "2026-02-10",
                "GAME_ID": "0022500001",
                "MATCHUP": "NYK @ BOS",
                "PTS": 40,
                "SEASON": "2025-26",
                "PLAYER_ID": 3,
                "PLAYER_NAME": "Road Knick",
                "INGESTED_AT_UTC": "2026-02-10T12:06:00+00:00",
            },
            {
                "GAME_DATE": "2026-02-11",
                "GAME_ID": "0022500002",
                "MATCHUP": "BOS @ PHI",
                "PTS": 15,
                "SEASON": "2025-26",
                "PLAYER_ID": 1,
                "PLAYER_NAME": "Test Scorer",
                "INGESTED_AT_UTC": "2026-02-11T12:00:00+00:00",
            },
        ]
    )


def test_parse_matchup_context_identifies_home_and_away():
    assert pipeline.parse_matchup_context("BOS vs. NYK") == {
        "team_abbr": "BOS",
        "opponent_abbr": "NYK",
        "home_away": "HOME",
    }
    assert pipeline.parse_matchup_context("LAL @ PHX") == {
        "team_abbr": "LAL",
        "opponent_abbr": "PHX",
        "home_away": "AWAY",
    }
    assert pipeline.parse_matchup_context("not a matchup") is None


def test_derive_game_line_scores_from_game_logs_uses_team_lookup_and_totals():
    result = pipeline.derive_game_line_scores_from_game_logs(
        _bootstrap_game_logs_sample()
    )

    first_game = result[result["GAME_ID"] == "0022500001"].sort_values("TEAM_ABBR")
    assert first_game["TEAM_ABBR"].tolist() == ["BOS", "NYK"]
    assert first_game["TEAM_ID"].tolist() == [1610612738, 1610612752]
    assert first_game["PTS"].tolist() == [50, 40]
    assert first_game["PTS_QTR1"].tolist() == [0, 0]


def test_derive_schedule_from_game_logs_marks_observed_home_away_and_b2b():
    result = pipeline.derive_schedule_from_game_logs(_bootstrap_game_logs_sample())

    first_game = result[result["GAME_ID"] == "0022500001"].sort_values("TEAM_ABBR")
    assert first_game[["TEAM_ABBR", "OPPONENT_ABBR", "HOME_AWAY"]].to_dict(
        "records"
    ) == [
        {"TEAM_ABBR": "BOS", "OPPONENT_ABBR": "NYK", "HOME_AWAY": "HOME"},
        {"TEAM_ABBR": "NYK", "OPPONENT_ABBR": "BOS", "HOME_AWAY": "AWAY"},
    ]
    boston_second_game = result[
        (result["GAME_ID"] == "0022500002") & (result["TEAM_ABBR"] == "BOS")
    ].iloc[0]
    assert bool(boston_second_game["IS_BACK_TO_BACK"]) is True


def test_derive_player_reference_from_game_logs_uses_latest_team_context():
    result = pipeline.derive_player_reference_from_game_logs(
        _bootstrap_game_logs_sample()
    )

    player = result[result["PLAYER_ID"] == 1].iloc[0]
    assert player["PLAYER_NAME"] == "Test Scorer"
    assert player["FIRST_NAME"] == "Test"
    assert player["LAST_NAME"] == "Scorer"
    assert player["TEAM_ABBR"] == "BOS"
    assert player["TEAM_ID"] == 1610612738
    assert bool(player["ROSTER_STATUS"]) is True


def test_should_bootstrap_bronze_table_modes():
    assert pipeline.should_bootstrap_bronze_table(
        "auto", raw_game_logs_rows=10, target_rows=None
    )
    assert pipeline.should_bootstrap_bronze_table(
        "auto", raw_game_logs_rows=10, target_rows=0
    )
    assert not pipeline.should_bootstrap_bronze_table(
        "auto", raw_game_logs_rows=10, target_rows=5
    )
    assert not pipeline.should_bootstrap_bronze_table(
        "off", raw_game_logs_rows=10, target_rows=0
    )
    assert pipeline.should_bootstrap_bronze_table(
        "force", raw_game_logs_rows=10, target_rows=5
    )
    assert not pipeline.should_bootstrap_bronze_table(
        "force", raw_game_logs_rows=0, target_rows=0
    )


def test_apply_bootstrap_domain_result_adds_accounting():
    current = {
        "domain": "schedule",
        "rows_loaded": 0,
        "rows_inserted": 0,
        "rows_updated": 0,
    }
    bootstrap = {
        "domain": "schedule",
        "ran": True,
        "rows_loaded": 4,
        "rows_inserted": 3,
        "rows_updated": 1,
        "rows_unchanged": 0,
        "dq_results": {"total_rows": 4},
        "reconciliation": {"inserted": 3},
    }

    result = pipeline.apply_bootstrap_domain_result(current, bootstrap)

    assert result["rows_loaded"] == 4
    assert result["rows_inserted"] == 3
    assert result["rows_updated"] == 1
    assert result["rows_unchanged"] == 0
    assert result["bronze_bootstrap"] == bootstrap
    assert result["bootstrap_dq_results"] == {"total_rows": 4}


def test_build_analysis_snapshot_record_is_deterministic():
    daily_leaders = pd.DataFrame(
        [
            {
                "season": "2025-26",
                "game_date": "2026-02-10",
                "pts_leader": "Jayson Tatum",
                "pts_matchup": "BOS vs. NYK",
                "pts": 34,
                "reb_leader": "Karl-Anthony Towns",
                "reb": 14,
                "ast_leader": "Trae Young",
                "ast": 11,
            }
        ]
    )
    trends = pd.DataFrame(
        [
            {
                "season": "2025-26",
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "stat": "PTS",
                "recent_games": 5,
                "prior_games": 5,
                "recent_avg": 28.4,
                "prior_avg": 22.0,
                "delta": 6.4,
                "pct_change": 29.1,
            }
        ]
    )

    record = pipeline.build_analysis_snapshot_record(
        season="2025-26",
        daily_leaders=daily_leaders,
        trends=trends,
        source_run_id="manual__2026-02-10T00:00:00+00:00",
        created_at_utc="2026-02-11T01:02:03+00:00",
        freshness_ts="2026-02-10T13:00:00+00:00",
    )

    assert record["snapshot_id"] == "202526_20260211"
    assert record["snapshot_date"] == "2026-02-11"
    assert record["season"] == "2025-26"
    assert record["trend_player"] == "Tyrese Maxey"
    assert record["trend_stat"] == "PTS"
    assert record["trend_delta"] == 6.4
    assert "Tyrese Maxey is trending up in PTS" in record["body"]
    assert "Jayson Tatum led scoring with 34 points" in record["body"]
    assert (
        "The latest completed game day in the 2025-26 warehouse is 2026-02-10."
        in record["body"]
    )


def test_build_analysis_snapshot_record_includes_fantasy_recommendations():
    daily_leaders = pd.DataFrame(
        [
            {
                "season": "2025-26",
                "game_date": "2026-02-10",
                "pts_leader": "Jayson Tatum",
                "pts_matchup": "BOS vs. NYK",
                "pts": 34,
                "reb_leader": "Karl-Anthony Towns",
                "reb": 14,
                "ast_leader": "Trae Young",
                "ast": 11,
            }
        ]
    )
    trends = pd.DataFrame(
        [
            {
                "season": "2025-26",
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "stat": "PTS",
                "recent_games": 5,
                "prior_games": 5,
                "recent_avg": 28.4,
                "prior_avg": 22.0,
                "delta": 6.4,
                "pct_change": 29.1,
            }
        ]
    )
    recommendations = pd.DataFrame(
        [
            {
                "player_name": "Tyrese Maxey",
                "insight_type": "waiver_add",
                "recommendation": "add",
                "priority_score": 94.0,
                "confidence_score": 88.0,
            }
        ]
    )
    rankings = pd.DataFrame(
        [
            {
                "player_name": "Nikola Jokic",
                "fantasy_rank_9cat_proxy": 1,
                "recommendation_tier": "strong_add",
            }
        ]
    )

    record = pipeline.build_analysis_snapshot_record(
        season="2025-26",
        daily_leaders=daily_leaders,
        trends=trends,
        recommendations=recommendations,
        rankings=rankings,
        source_run_id="manual__2026-02-10T00:00:00+00:00",
        created_at_utc="2026-02-11T01:02:03+00:00",
        freshness_ts="2026-02-10T13:00:00+00:00",
    )

    assert record["headline"] == "Tyrese Maxey headlines the 2025-26 fantasy board"
    assert "Top fantasy signal: Tyrese Maxey profiles as waiver_add" in record["body"]
    assert "Current fantasy leader: Nikola Jokic sits at rank 1" in record["body"]


def test_build_analysis_snapshot_record_includes_scoring_contribution_and_context():
    daily_leaders = pd.DataFrame(
        [
            {
                "season": "2025-26",
                "game_date": "2026-02-10",
                "pts_leader": "Jayson Tatum",
                "pts_matchup": "BOS vs. NYK",
                "pts": 34,
                "reb_leader": "Karl-Anthony Towns",
                "reb": 14,
                "ast_leader": "Trae Young",
                "ast": 11,
            }
        ]
    )
    trends = pd.DataFrame(
        [
            {
                "season": "2025-26",
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "stat": "PTS",
                "recent_games": 5,
                "prior_games": 5,
                "recent_avg": 28.4,
                "prior_avg": 22.0,
                "delta": 6.4,
                "pct_change": 29.1,
            }
        ]
    )
    score_contribution = pd.DataFrame(
        [
            {
                "season": "2025-26",
                "game_id": "001",
                "game_date": "2026-02-10",
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "team_abbr": "PHI",
                "opponent_abbr": "NYK",
                "matchup": "PHI vs. NYK",
                "player_pts": 31,
                "team_pts": 112,
                "opponent_team_pts": 108,
                "team_pts_qtr1": 28,
                "team_pts_qtr2": 24,
                "team_pts_qtr3": 30,
                "team_pts_qtr4": 30,
                "team_pts_ot_total": 0,
                "scoring_margin": 4,
                "player_points_share_of_team": 0.2768,
                "player_points_share_of_game": 0.1416,
            }
        ]
    )
    player_context = pd.DataFrame(
        [
            {
                "player_id": 7,
                "player_name": "Tyrese Maxey",
                "latest_team_abbr": "PHI",
                "team_name": "76ers",
                "position": "G",
                "height": "6-2",
                "weight": 200,
                "roster_status": True,
                "season_exp": 5,
                "draft_year": "2020",
                "draft_round": "1",
                "draft_number": "21",
            }
        ]
    )

    record = pipeline.build_analysis_snapshot_record(
        season="2025-26",
        daily_leaders=daily_leaders,
        trends=trends,
        score_contribution=score_contribution,
        player_context=player_context,
        source_run_id="manual__2026-02-10T00:00:00+00:00",
        created_at_utc="2026-02-11T01:02:03+00:00",
        freshness_ts="2026-02-10T13:00:00+00:00",
    )

    assert record["contribution_player_id"] == 7
    assert record["contribution_player_name"] == "Tyrese Maxey"
    assert record["contribution_player_points_share_of_team"] == 0.2768
    assert record["contribution_team_pts_qtr4"] == 30
    assert record["context_player_id"] == 7
    assert record["context_position"] == "G"
    assert record["context_roster_status"] is True
    assert "Tyrese Maxey supplied 31 of 112 PHI points" in record["body"]
    assert "Tyrese Maxey is listed as a G for 76ers (PHI)" in record["body"]


def test_build_player_similarity_outputs_returns_normalized_feature_tables():
    feature_rows = pd.DataFrame(
        [
            {
                "season": "2025-26",
                "as_of_date": "2026-02-11",
                "player_id": player_id,
                "player_name": player_name,
                "team_abbr": team_abbr,
                "position": position,
                "games_sampled": 24,
                "sample_status": "ready",
                "season_avg_pts": season_avg_pts,
                "season_avg_reb": season_avg_reb,
                "season_avg_ast": season_avg_ast,
                "season_avg_stl": season_avg_stl,
                "season_avg_blk": season_avg_blk,
                "season_avg_fg3m": season_avg_fg3m,
                "season_avg_tov": season_avg_tov,
                "season_avg_min": season_avg_min,
                "recent_pts": recent_pts,
                "recent_reb": recent_reb,
                "recent_ast": recent_ast,
                "recent_stl": recent_stl,
                "recent_blk": recent_blk,
                "recent_fg3m": recent_fg3m,
                "recent_tov": recent_tov,
                "recent_min": recent_min,
                "recent_points_share_of_team": recent_points_share_of_team,
                "recent_points_share_of_game": recent_points_share_of_game,
                "minutes_delta_vs_season": minutes_delta_vs_season,
            }
            for (
                player_id,
                player_name,
                team_abbr,
                position,
                season_avg_pts,
                season_avg_reb,
                season_avg_ast,
                season_avg_stl,
                season_avg_blk,
                season_avg_fg3m,
                season_avg_tov,
                season_avg_min,
                recent_pts,
                recent_reb,
                recent_ast,
                recent_stl,
                recent_blk,
                recent_fg3m,
                recent_tov,
                recent_min,
                recent_points_share_of_team,
                recent_points_share_of_game,
                minutes_delta_vs_season,
            ) in [
                (
                    1,
                    "Tyrese Maxey",
                    "PHI",
                    "G",
                    25.0,
                    4.2,
                    7.1,
                    1.2,
                    0.3,
                    3.2,
                    2.4,
                    35.8,
                    28.4,
                    4.8,
                    7.6,
                    1.4,
                    0.2,
                    3.5,
                    2.2,
                    36.4,
                    0.28,
                    0.14,
                    1.6,
                ),
                (
                    2,
                    "Jalen Brunson",
                    "NYK",
                    "G",
                    26.2,
                    3.6,
                    6.9,
                    1.0,
                    0.2,
                    2.7,
                    2.6,
                    35.2,
                    27.8,
                    3.9,
                    7.3,
                    1.1,
                    0.2,
                    2.9,
                    2.4,
                    35.8,
                    0.29,
                    0.15,
                    1.1,
                ),
                (
                    3,
                    "Mikal Bridges",
                    "NYK",
                    "F",
                    19.3,
                    4.7,
                    3.6,
                    1.1,
                    0.7,
                    2.4,
                    1.6,
                    35.4,
                    20.2,
                    4.9,
                    3.3,
                    1.3,
                    0.8,
                    2.6,
                    1.5,
                    35.6,
                    0.21,
                    0.10,
                    0.5,
                ),
                (
                    4,
                    "Jaren Jackson Jr.",
                    "MEM",
                    "F-C",
                    22.1,
                    6.4,
                    2.1,
                    1.0,
                    1.9,
                    1.8,
                    1.9,
                    32.5,
                    23.4,
                    6.8,
                    2.4,
                    1.1,
                    2.1,
                    2.0,
                    1.8,
                    33.1,
                    0.25,
                    0.12,
                    0.8,
                ),
                (
                    5,
                    "Brook Lopez",
                    "MIL",
                    "C",
                    14.8,
                    6.1,
                    1.4,
                    0.6,
                    2.3,
                    2.1,
                    1.4,
                    29.5,
                    15.4,
                    6.3,
                    1.6,
                    0.7,
                    2.5,
                    2.2,
                    1.5,
                    30.2,
                    0.19,
                    0.09,
                    0.6,
                ),
                (
                    6,
                    "Josh Hart",
                    "NYK",
                    "F",
                    14.2,
                    9.1,
                    5.4,
                    1.4,
                    0.5,
                    1.3,
                    1.8,
                    37.4,
                    15.3,
                    10.1,
                    5.9,
                    1.5,
                    0.6,
                    1.6,
                    1.6,
                    38.1,
                    0.18,
                    0.08,
                    1.4,
                ),
            ]
        ]
    )

    outputs = pipeline.build_player_similarity_outputs(feature_rows, cluster_count=4)

    assert set(outputs) == {"features", "archetypes"}
    assert len(outputs["features"]) == 6
    assert len(outputs["archetypes"]) == 6
    assert not outputs["features"].duplicated(subset=["season", "player_id"]).any()
    assert set(outputs["archetypes"]["archetype_label"]).issubset(
        pipeline.ALLOWED_ARCHETYPE_LABELS
    )
    for feature_name in pipeline.SIMILARITY_FEATURE_COLUMNS:
        assert f"norm_{feature_name}" in outputs["features"].columns
    assert outputs["archetypes"]["top_traits"].str.len().gt(0).all()


def test_build_player_similarity_outputs_excludes_insufficient_sample_rows():
    feature_rows = pd.DataFrame(
        [
            {
                "season": "2025-26",
                "as_of_date": "2026-02-11",
                "player_id": 1,
                "player_name": "Ready Player",
                "team_abbr": "PHI",
                "position": "G",
                "games_sampled": 20,
                "sample_status": "ready",
                **{
                    feature_name: 1.0
                    for feature_name in pipeline.SIMILARITY_FEATURE_COLUMNS
                },
            },
            {
                "season": "2025-26",
                "as_of_date": "2026-02-11",
                "player_id": 2,
                "player_name": "Limited Player",
                "team_abbr": "NYK",
                "position": "F",
                "games_sampled": 6,
                "sample_status": "limited_sample",
                **{
                    feature_name: 2.0
                    for feature_name in pipeline.SIMILARITY_FEATURE_COLUMNS
                },
            },
            {
                "season": "2025-26",
                "as_of_date": "2026-02-11",
                "player_id": 3,
                "player_name": "Insufficient Player",
                "team_abbr": "BOS",
                "position": "C",
                "games_sampled": 2,
                "sample_status": "insufficient_sample",
                **{
                    feature_name: 3.0
                    for feature_name in pipeline.SIMILARITY_FEATURE_COLUMNS
                },
            },
        ]
    )

    outputs = pipeline.build_player_similarity_outputs(feature_rows, cluster_count=3)

    assert outputs["features"]["player_id"].tolist() == [1, 2]
    assert outputs["archetypes"]["player_id"].tolist() == [1, 2]
