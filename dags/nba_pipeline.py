"""NBA pipeline business logic.

This module stays free of Airflow imports so its helpers remain unit-testable.
It supports incremental extraction, metadata persistence, deterministic
analysis snapshot generation, and idempotent warehouse loads.
"""

from __future__ import annotations

import logging
import json
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from io import BytesIO
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
from google.api_core.exceptions import NotFound
from google.cloud import bigquery, storage
from nba_api.stats.endpoints import boxscoresummaryv2
from nba_api.stats.endpoints import commonplayerinfo
from nba_api.stats.endpoints import playergamelog
from nba_api.stats.endpoints import scheduleleaguev2
from nba_api.stats.static import players

logger = logging.getLogger("nba_pipeline")

SOURCE_SYSTEM = "nba_api"
INJURY_REPORT_SOURCE_SYSTEM = "nba_official_injury_report"
OFFICIAL_INJURY_REPORT_BASE_URL = "https://ak-static.cms.nba.com/referee/injury"
DEFAULT_INJURY_REPORT_TIME_ET = "05_00PM"
DEFAULT_PLAYOFF_BACKFILL_START = date(2026, 4, 18)
OFFICIAL_INJURY_REPORT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,application/octet-stream,*/*",
}
SUPPORTED_SEASON = "2025-26"
SUPPORTED_SEASON_START = date(2025, 7, 1)
SUPPORTED_SEASON_END = date(2026, 6, 30)
NBA_API_TIMEOUT_SECONDS = 15.0
NBA_API_RETRIES = 3
NBA_API_RETRY_BASE_DELAY_SECONDS = 1.0
NBA_API_RETRY_BACKOFF_MULTIPLIER = 2.0
NBA_API_RETRY_MAX_DELAY_SECONDS = 8.0

NBA_TEAM_LOOKUP_ROWS: Tuple[Tuple[str, int, str, str], ...] = (
    ("ATL", 1610612737, "Atlanta", "Hawks"),
    ("BOS", 1610612738, "Boston", "Celtics"),
    ("BKN", 1610612751, "Brooklyn", "Nets"),
    ("CHA", 1610612766, "Charlotte", "Hornets"),
    ("CHI", 1610612741, "Chicago", "Bulls"),
    ("CLE", 1610612739, "Cleveland", "Cavaliers"),
    ("DAL", 1610612742, "Dallas", "Mavericks"),
    ("DEN", 1610612743, "Denver", "Nuggets"),
    ("DET", 1610612765, "Detroit", "Pistons"),
    ("GSW", 1610612744, "Golden State", "Warriors"),
    ("HOU", 1610612745, "Houston", "Rockets"),
    ("IND", 1610612754, "Indiana", "Pacers"),
    ("LAC", 1610612746, "LA", "Clippers"),
    ("LAL", 1610612747, "Los Angeles", "Lakers"),
    ("MEM", 1610612763, "Memphis", "Grizzlies"),
    ("MIA", 1610612748, "Miami", "Heat"),
    ("MIL", 1610612749, "Milwaukee", "Bucks"),
    ("MIN", 1610612750, "Minnesota", "Timberwolves"),
    ("NOP", 1610612740, "New Orleans", "Pelicans"),
    ("NYK", 1610612752, "New York", "Knicks"),
    ("OKC", 1610612760, "Oklahoma City", "Thunder"),
    ("ORL", 1610612753, "Orlando", "Magic"),
    ("PHI", 1610612755, "Philadelphia", "76ers"),
    ("PHX", 1610612756, "Phoenix", "Suns"),
    ("POR", 1610612757, "Portland", "Trail Blazers"),
    ("SAC", 1610612758, "Sacramento", "Kings"),
    ("SAS", 1610612759, "San Antonio", "Spurs"),
    ("TOR", 1610612761, "Toronto", "Raptors"),
    ("UTA", 1610612762, "Utah", "Jazz"),
    ("WAS", 1610612764, "Washington", "Wizards"),
)
NBA_TEAM_LOOKUP: Tuple[Dict[str, Any], ...] = tuple(
    {
        "team_abbr": team_abbr,
        "team_id": team_id,
        "team_city_name": team_city_name,
        "team_nickname": team_nickname,
    }
    for team_abbr, team_id, team_city_name, team_nickname in NBA_TEAM_LOOKUP_ROWS
)
NBA_TEAM_LOOKUP_BY_ABBR = {team["team_abbr"]: team for team in NBA_TEAM_LOOKUP}
NBA_TEAM_NAME_TO_ABBR = {
    f"{team['team_city_name']} {team['team_nickname']}".upper(): team["team_abbr"]
    for team in NBA_TEAM_LOOKUP
}
NBA_TEAM_NAME_TO_ABBR["LOS ANGELES CLIPPERS"] = "LAC"
NBA_TEAM_NAME_TO_ABBR["LA CLIPPERS"] = "LAC"
NBA_TEAM_NAME_CANONICAL = {
    f"{team['team_city_name']} {team['team_nickname']}".upper(): (
        f"{team['team_city_name']} {team['team_nickname']}"
    )
    for team in NBA_TEAM_LOOKUP
}
NBA_TEAM_NAME_CANONICAL["LOS ANGELES CLIPPERS"] = "Los Angeles Clippers"
NBA_TEAM_NAME_CANONICAL["LA CLIPPERS"] = "LA Clippers"


def get_season_date_bounds(season: str = SUPPORTED_SEASON) -> Tuple[date, date]:
    """Return the inclusive date bounds for the supported production season."""
    if season != SUPPORTED_SEASON:
        raise ValueError(f"Unsupported production season: {season}")
    return SUPPORTED_SEASON_START, SUPPORTED_SEASON_END


def coerce_to_date(value: Any) -> Optional[date]:
    """Convert supported date-like values to a date."""
    if value in (None, "", pd.NaT):
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def compute_replay_start(watermark_date: Any, replay_days: int = 3) -> Optional[date]:
    """Return the inclusive replay start date for an existing watermark."""
    watermark = coerce_to_date(watermark_date)
    if watermark is None:
        return None
    replay_window = max(replay_days - 1, 0)
    return watermark - timedelta(days=replay_window)


def filter_incremental_game_logs(
    df: pd.DataFrame,
    watermark_date: Any = None,
    replay_days: int = 3,
    season: str = SUPPORTED_SEASON,
) -> pd.DataFrame:
    """Keep rows inside the replay window and normalize key fields."""
    if df.empty:
        return df.copy()

    working = df.copy()
    working["GAME_DATE"] = pd.to_datetime(
        working["GAME_DATE"], errors="coerce"
    ).dt.normalize()
    working = working.dropna(subset=["GAME_DATE"]).copy()
    if "SEASON" not in working.columns:
        working["SEASON"] = season
    if "WL" in working.columns:
        working["WL"] = working["WL"].astype("string").str.upper()
    if "SEASON" in working.columns:
        working["SEASON"] = working["SEASON"].astype("string")
        working = working[working["SEASON"] == season].copy()

    season_start, season_end = get_season_date_bounds(season)
    working = working[
        (working["GAME_DATE"].dt.date >= season_start)
        & (working["GAME_DATE"].dt.date <= season_end)
    ].copy()

    replay_start = compute_replay_start(watermark_date, replay_days=replay_days)
    if replay_start is not None:
        working = working[working["GAME_DATE"].dt.date >= replay_start].copy()

    working = working.drop_duplicates(
        subset=["PLAYER_ID", "GAME_DATE", "MATCHUP"]
    ).copy()
    working = working.sort_values(["GAME_DATE", "PLAYER_ID"], ascending=[False, True])
    return working.reset_index(drop=True)


def build_run_metadata_record(
    *,
    dag_run_id: str,
    season: str,
    status: str,
    source_system: str = SOURCE_SYSTEM,
    gcs_uri: str = "",
    rows_extracted: int = 0,
    rows_loaded: int = 0,
    rows_inserted: int = 0,
    rows_updated: int = 0,
    watermark_before: Any = None,
    watermark_after: Any = None,
    started_at_utc: Any = None,
    finished_at_utc: Any = None,
    details: str = "",
) -> Dict[str, Any]:
    """Build a JSON-serializable metadata record for a pipeline run."""
    started = pd.to_datetime(started_at_utc or pd.Timestamp.now(tz="UTC"), utc=True)
    finished = pd.to_datetime(finished_at_utc or pd.Timestamp.now(tz="UTC"), utc=True)
    return {
        "dag_run_id": dag_run_id,
        "source_system": source_system,
        "season": season,
        "status": status,
        "gcs_uri": gcs_uri,
        "rows_extracted": int(rows_extracted),
        "rows_loaded": int(rows_loaded),
        "rows_inserted": int(rows_inserted),
        "rows_updated": int(rows_updated),
        "watermark_before": coerce_to_date(watermark_before).isoformat()
        if coerce_to_date(watermark_before)
        else None,
        "watermark_after": coerce_to_date(watermark_after).isoformat()
        if coerce_to_date(watermark_after)
        else None,
        "started_at_utc": started.isoformat(),
        "finished_at_utc": finished.isoformat(),
        "details": details,
    }


def ensure_dataset(bq_client: bigquery.Client, dataset_id: str, location: str) -> None:
    """Create the dataset if it does not exist."""
    dataset = bigquery.Dataset(dataset_id)
    dataset.location = location
    bq_client.create_dataset(dataset, exists_ok=True)


def create_metadata_tables(
    bq_client: bigquery.Client,
    state_table: str,
    run_metadata_table: str,
) -> None:
    """Create metadata tables used for watermarking and run tracking."""
    state_sql = f"""
    CREATE TABLE IF NOT EXISTS `{state_table}` (
      source_system STRING,
      season STRING,
      watermark_date DATE,
      updated_at_utc TIMESTAMP
    )
    """
    run_sql = f"""
    CREATE TABLE IF NOT EXISTS `{run_metadata_table}` (
      dag_run_id STRING,
      source_system STRING,
      season STRING,
      status STRING,
      gcs_uri STRING,
      rows_extracted INT64,
      rows_loaded INT64,
      rows_inserted INT64,
      rows_updated INT64,
      watermark_before DATE,
      watermark_after DATE,
      started_at_utc TIMESTAMP,
      finished_at_utc TIMESTAMP,
      details STRING
    )
    PARTITION BY DATE(started_at_utc)
    """
    bq_client.query(state_sql).result()
    bq_client.query(run_sql).result()


def get_ingestion_state(
    bq_client: bigquery.Client,
    state_table: str,
    *,
    source_system: str = SOURCE_SYSTEM,
    season: str,
) -> Dict[str, Optional[date]]:
    """Fetch the current watermark for a source/season pair."""
    query = f"""
    SELECT watermark_date, updated_at_utc
    FROM `{state_table}`
    WHERE source_system = @source_system
      AND season = @season
    ORDER BY updated_at_utc DESC
    LIMIT 1
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("source_system", "STRING", source_system),
            bigquery.ScalarQueryParameter("season", "STRING", season),
        ]
    )
    rows = list(bq_client.query(query, job_config=job_config).result())
    if not rows:
        return {"watermark_date": None, "updated_at_utc": None}
    row = rows[0]
    return {
        "watermark_date": coerce_to_date(row["watermark_date"]),
        "updated_at_utc": row["updated_at_utc"],
    }


def upsert_ingestion_state(
    bq_client: bigquery.Client,
    state_table: str,
    *,
    season: str,
    watermark_date: Any,
    source_system: str = SOURCE_SYSTEM,
) -> None:
    """Persist the latest successful watermark."""
    watermark = coerce_to_date(watermark_date)
    if watermark is None:
        return

    merge_sql = f"""
    MERGE `{state_table}` T
    USING (
      SELECT
        @source_system AS source_system,
        @season AS season,
        @watermark_date AS watermark_date,
        CURRENT_TIMESTAMP() AS updated_at_utc
    ) S
    ON T.source_system = S.source_system
    AND T.season = S.season
    WHEN MATCHED THEN
      UPDATE SET
        watermark_date = CASE
          WHEN T.watermark_date IS NULL OR S.watermark_date > T.watermark_date
            THEN S.watermark_date
          ELSE T.watermark_date
        END,
        updated_at_utc = S.updated_at_utc
    WHEN NOT MATCHED THEN
      INSERT (source_system, season, watermark_date, updated_at_utc)
      VALUES (S.source_system, S.season, S.watermark_date, S.updated_at_utc)
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("source_system", "STRING", source_system),
            bigquery.ScalarQueryParameter("season", "STRING", season),
            bigquery.ScalarQueryParameter(
                "watermark_date", "DATE", watermark.isoformat()
            ),
        ]
    )
    bq_client.query(merge_sql, job_config=job_config).result()


def record_pipeline_run(
    bq_client: bigquery.Client,
    run_metadata_table: str,
    record: Dict[str, Any],
) -> None:
    """Insert a single metadata record into BigQuery."""
    errors = bq_client.insert_rows_json(run_metadata_table, [record])
    if errors:
        raise RuntimeError(f"Failed to record pipeline run: {errors}")


def create_source_contract_metadata_tables(
    bq_client: bigquery.Client,
    source_contract_results_table: str,
) -> None:
    """Create metadata tables used for source contract audit results."""
    results_sql = f"""
    CREATE TABLE IF NOT EXISTS `{source_contract_results_table}` (
      dag_run_id STRING,
      domain STRING,
      source_name STRING,
      contract_version STRING,
      status STRING,
      reason STRING,
      rows_checked INT64,
      rows_failed INT64,
      rows_quarantined INT64,
      fatal_count INT64,
      warning_count INT64,
      quarantine_count INT64,
      raw_snapshot_uri STRING,
      quarantine_uri STRING,
      landing_uri STRING,
      validated_at_utc TIMESTAMP,
      details_json STRING
    )
    PARTITION BY DATE(validated_at_utc)
    CLUSTER BY domain, status
    """
    bq_client.query(results_sql).result()


def build_source_contract_result_record(
    *,
    dag_run_id: str,
    result: Dict[str, Any],
    raw_snapshot_uri: str = "",
    quarantine_uri: str = "",
    landing_uri: str = "",
    validated_at_utc: Any = None,
) -> Dict[str, Any]:
    """Build a BigQuery-friendly source contract audit record."""
    validated_at = pd.to_datetime(
        validated_at_utc or pd.Timestamp.now(tz="UTC"), utc=True
    )
    details = dict(result)
    details["raw_snapshot_uri"] = raw_snapshot_uri
    details["quarantine_uri"] = quarantine_uri
    details["landing_uri"] = landing_uri
    return {
        "dag_run_id": dag_run_id,
        "domain": str(result.get("domain", "")),
        "source_name": str(result.get("source_name", "")),
        "contract_version": str(result.get("contract_version", "")),
        "status": str(result.get("status", "")),
        "reason": str(result.get("reason", "")),
        "rows_checked": int(result.get("rows_checked", 0) or 0),
        "rows_failed": int(result.get("rows_failed", 0) or 0),
        "rows_quarantined": int(result.get("rows_quarantined", 0) or 0),
        "fatal_count": int(result.get("fatal_count", 0) or 0),
        "warning_count": int(result.get("warning_count", 0) or 0),
        "quarantine_count": int(result.get("quarantine_count", 0) or 0),
        "raw_snapshot_uri": raw_snapshot_uri,
        "quarantine_uri": quarantine_uri,
        "landing_uri": landing_uri,
        "validated_at_utc": validated_at.isoformat(),
        "details_json": json.dumps(details, sort_keys=True, default=str),
    }


def record_source_contract_result(
    bq_client: bigquery.Client,
    source_contract_results_table: str,
    record: Dict[str, Any],
) -> None:
    """Upsert a source contract audit record for a DAG run/domain pair."""
    merge_sql = f"""
    MERGE `{source_contract_results_table}` T
    USING (
      SELECT
        @dag_run_id AS dag_run_id,
        @domain AS domain,
        @source_name AS source_name,
        @contract_version AS contract_version,
        @status AS status,
        @reason AS reason,
        @rows_checked AS rows_checked,
        @rows_failed AS rows_failed,
        @rows_quarantined AS rows_quarantined,
        @fatal_count AS fatal_count,
        @warning_count AS warning_count,
        @quarantine_count AS quarantine_count,
        @raw_snapshot_uri AS raw_snapshot_uri,
        @quarantine_uri AS quarantine_uri,
        @landing_uri AS landing_uri,
        @validated_at_utc AS validated_at_utc,
        @details_json AS details_json
    ) S
    ON T.dag_run_id = S.dag_run_id
    AND T.domain = S.domain
    AND T.contract_version = S.contract_version
    WHEN MATCHED THEN UPDATE SET
      source_name = S.source_name,
      status = S.status,
      reason = S.reason,
      rows_checked = S.rows_checked,
      rows_failed = S.rows_failed,
      rows_quarantined = S.rows_quarantined,
      fatal_count = S.fatal_count,
      warning_count = S.warning_count,
      quarantine_count = S.quarantine_count,
      raw_snapshot_uri = S.raw_snapshot_uri,
      quarantine_uri = S.quarantine_uri,
      landing_uri = S.landing_uri,
      validated_at_utc = S.validated_at_utc,
      details_json = S.details_json
    WHEN NOT MATCHED THEN
      INSERT (
        dag_run_id, domain, source_name, contract_version, status, reason,
        rows_checked, rows_failed, rows_quarantined, fatal_count, warning_count,
        quarantine_count, raw_snapshot_uri, quarantine_uri, landing_uri,
        validated_at_utc, details_json
      )
      VALUES (
        S.dag_run_id, S.domain, S.source_name, S.contract_version, S.status, S.reason,
        S.rows_checked, S.rows_failed, S.rows_quarantined, S.fatal_count,
        S.warning_count, S.quarantine_count, S.raw_snapshot_uri, S.quarantine_uri,
        S.landing_uri, S.validated_at_utc, S.details_json
      )
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("dag_run_id", "STRING", record["dag_run_id"]),
            bigquery.ScalarQueryParameter("domain", "STRING", record["domain"]),
            bigquery.ScalarQueryParameter(
                "source_name", "STRING", record["source_name"]
            ),
            bigquery.ScalarQueryParameter(
                "contract_version", "STRING", record["contract_version"]
            ),
            bigquery.ScalarQueryParameter("status", "STRING", record["status"]),
            bigquery.ScalarQueryParameter("reason", "STRING", record["reason"]),
            bigquery.ScalarQueryParameter(
                "rows_checked", "INT64", record["rows_checked"]
            ),
            bigquery.ScalarQueryParameter(
                "rows_failed", "INT64", record["rows_failed"]
            ),
            bigquery.ScalarQueryParameter(
                "rows_quarantined", "INT64", record["rows_quarantined"]
            ),
            bigquery.ScalarQueryParameter(
                "fatal_count", "INT64", record["fatal_count"]
            ),
            bigquery.ScalarQueryParameter(
                "warning_count", "INT64", record["warning_count"]
            ),
            bigquery.ScalarQueryParameter(
                "quarantine_count", "INT64", record["quarantine_count"]
            ),
            bigquery.ScalarQueryParameter(
                "raw_snapshot_uri", "STRING", record["raw_snapshot_uri"]
            ),
            bigquery.ScalarQueryParameter(
                "quarantine_uri", "STRING", record["quarantine_uri"]
            ),
            bigquery.ScalarQueryParameter(
                "landing_uri", "STRING", record["landing_uri"]
            ),
            bigquery.ScalarQueryParameter(
                "validated_at_utc", "TIMESTAMP", record["validated_at_utc"]
            ),
            bigquery.ScalarQueryParameter(
                "details_json", "STRING", record["details_json"]
            ),
        ]
    )
    bq_client.query(merge_sql, job_config=job_config).result()


def get_active_players() -> list:
    """Return all active NBA players from the NBA API."""
    active = players.get_active_players()
    logger.info("Found %s active players", len(active))
    return active


def normalize_nba_api_retries(retries: int) -> int:
    """Keep endpoint retry counts in a valid operational range."""
    return max(int(retries), 1)


def calculate_nba_api_retry_delay(
    attempt: int,
    *,
    base_delay: float = NBA_API_RETRY_BASE_DELAY_SECONDS,
    backoff_multiplier: float = NBA_API_RETRY_BACKOFF_MULTIPLIER,
    max_delay: float = NBA_API_RETRY_MAX_DELAY_SECONDS,
) -> float:
    """Return bounded exponential sleep seconds after a failed attempt."""
    safe_attempt = max(int(attempt), 1)
    safe_base_delay = max(float(base_delay), 0.0)
    safe_multiplier = max(float(backoff_multiplier), 1.0)
    safe_max_delay = max(float(max_delay), 0.0)
    delay = safe_base_delay * (safe_multiplier ** (safe_attempt - 1))
    return min(delay, safe_max_delay)


def _sleep_before_nba_api_retry(
    *,
    domain: str,
    identifier: Any,
    attempt: int,
    retries: int,
    timeout: float,
    retry_base_delay: float,
    retry_backoff_multiplier: float,
    retry_max_delay: float,
) -> None:
    sleep_seconds = calculate_nba_api_retry_delay(
        attempt,
        base_delay=retry_base_delay,
        backoff_multiplier=retry_backoff_multiplier,
        max_delay=retry_max_delay,
    )
    logger.warning(
        "Retrying NBA API %s id=%s attempt=%s/%s timeout=%.1fs in %.1fs",
        domain,
        identifier,
        attempt,
        retries,
        timeout,
        sleep_seconds,
    )
    time.sleep(sleep_seconds)


def get_player_game_log(
    player_id: int,
    season: str = SUPPORTED_SEASON,
    retries: int = NBA_API_RETRIES,
    delay: Optional[float] = None,
    timeout: float = NBA_API_TIMEOUT_SECONDS,
    retry_base_delay: float = NBA_API_RETRY_BASE_DELAY_SECONDS,
    retry_backoff_multiplier: float = NBA_API_RETRY_BACKOFF_MULTIPLIER,
    retry_max_delay: float = NBA_API_RETRY_MAX_DELAY_SECONDS,
) -> pd.DataFrame:
    """Get normalized game logs for a single player with retry logic."""
    retries = normalize_nba_api_retries(retries)
    if delay is not None:
        retry_base_delay = delay
    cols = [
        "Game_ID",
        "GAME_DATE",
        "MATCHUP",
        "WL",
        "MIN",
        "FGM",
        "FGA",
        "FG_PCT",
        "FG3M",
        "FG3A",
        "FG3_PCT",
        "FTM",
        "FTA",
        "FT_PCT",
        "OREB",
        "DREB",
        "PTS",
        "REB",
        "AST",
        "STL",
        "BLK",
        "TOV",
        "PF",
        "PLUS_MINUS",
    ]

    for attempt in range(1, retries + 1):
        try:
            gamelog = playergamelog.PlayerGameLog(
                player_id=player_id,
                season=season,
                timeout=timeout,
            )
            df = gamelog.get_data_frames()[0]

            missing_cols = [c for c in cols if c not in df.columns]
            if missing_cols:
                raise ValueError(f"Missing expected columns: {missing_cols}")

            out = df[cols].copy()
            out = out.rename(columns={"Game_ID": "GAME_ID"})
            out["GAME_DATE"] = pd.to_datetime(out["GAME_DATE"], errors="coerce")
            out = out.dropna(subset=["GAME_DATE"])

            numeric_cols = [
                "MIN",
                "FGM",
                "FGA",
                "FG_PCT",
                "FG3M",
                "FG3A",
                "FG3_PCT",
                "FTM",
                "FTA",
                "FT_PCT",
                "OREB",
                "DREB",
                "PTS",
                "REB",
                "AST",
                "STL",
                "BLK",
                "TOV",
                "PF",
                "PLUS_MINUS",
            ]
            for col in numeric_cols:
                out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0)

            out["SEASON"] = season
            out["INGESTED_AT_UTC"] = pd.Timestamp.now(tz="UTC")
            return out
        except Exception:
            if attempt == retries:
                logger.exception(
                    "Failed NBA API player game log player_id=%s after %s attempts timeout=%.1fs",
                    player_id,
                    retries,
                    timeout,
                )
                return pd.DataFrame()
            _sleep_before_nba_api_retry(
                domain="player_game_log",
                identifier=player_id,
                attempt=attempt,
                retries=retries,
                timeout=timeout,
                retry_base_delay=retry_base_delay,
                retry_backoff_multiplier=retry_backoff_multiplier,
                retry_max_delay=retry_max_delay,
            )

    return pd.DataFrame()


def get_all_player_game_logs(
    player_list: Iterable[dict],
    season: str = SUPPORTED_SEASON,
    delay: float = 0.6,
    retries: int = NBA_API_RETRIES,
    timeout: float = NBA_API_TIMEOUT_SECONDS,
    retry_base_delay: float = NBA_API_RETRY_BASE_DELAY_SECONDS,
    retry_backoff_multiplier: float = NBA_API_RETRY_BACKOFF_MULTIPLIER,
    retry_max_delay: float = NBA_API_RETRY_MAX_DELAY_SECONDS,
) -> pd.DataFrame:
    """Fetch game logs for multiple players with rate limiting."""
    all_logs = []
    player_list = list(player_list)

    for i, player in enumerate(player_list, start=1):
        player_id = player["id"]
        player_name = player["full_name"]
        logger.info("Fetching %s/%s: %s", i, len(player_list), player_name)

        games = get_player_game_log(
            player_id,
            season=season,
            retries=retries,
            timeout=timeout,
            retry_base_delay=retry_base_delay,
            retry_backoff_multiplier=retry_backoff_multiplier,
            retry_max_delay=retry_max_delay,
        )
        if not games.empty:
            games["PLAYER_ID"] = player_id
            games["PLAYER_NAME"] = player_name
            all_logs.append(games)

        time.sleep(delay)

    if not all_logs:
        raise RuntimeError(
            "No game logs were fetched. Check API availability and season value."
        )

    all_game_logs = pd.concat(all_logs, ignore_index=True)
    all_game_logs = all_game_logs.drop_duplicates(
        subset=["PLAYER_ID", "GAME_DATE", "MATCHUP"]
    ).copy()
    all_game_logs = all_game_logs.sort_values(
        ["GAME_DATE", "PLAYER_ID"], ascending=[False, True]
    )

    required = {
        "GAME_DATE",
        "PLAYER_ID",
        "PLAYER_NAME",
        "FGM",
        "FGA",
        "FG_PCT",
        "FG3M",
        "FG3A",
        "FG3_PCT",
        "FTM",
        "FTA",
        "FT_PCT",
        "PTS",
        "REB",
        "AST",
        "STL",
        "BLK",
    }
    missing = required - set(all_game_logs.columns)
    if missing:
        raise ValueError(f"Missing required fields in merged logs: {sorted(missing)}")

    logger.info(
        "Fetched %s rows across %s players",
        len(all_game_logs),
        all_game_logs["PLAYER_ID"].nunique(),
    )
    return all_game_logs.reset_index(drop=True)


def get_game_line_scores(
    game_id: str,
    *,
    season: str = SUPPORTED_SEASON,
    retries: int = NBA_API_RETRIES,
    delay: Optional[float] = None,
    timeout: float = NBA_API_TIMEOUT_SECONDS,
    retry_base_delay: float = NBA_API_RETRY_BASE_DELAY_SECONDS,
    retry_backoff_multiplier: float = NBA_API_RETRY_BACKOFF_MULTIPLIER,
    retry_max_delay: float = NBA_API_RETRY_MAX_DELAY_SECONDS,
) -> pd.DataFrame:
    """Get normalized team line scores for a single game."""
    retries = normalize_nba_api_retries(retries)
    if delay is not None:
        retry_base_delay = delay
    line_score_fields = [
        "GAME_DATE_EST",
        "GAME_ID",
        "TEAM_ID",
        "TEAM_ABBREVIATION",
        "TEAM_CITY_NAME",
        "TEAM_NICKNAME",
        "TEAM_WINS_LOSSES",
        "PTS_QTR1",
        "PTS_QTR2",
        "PTS_QTR3",
        "PTS_QTR4",
        "PTS_OT1",
        "PTS_OT2",
        "PTS_OT3",
        "PTS_OT4",
        "PTS_OT5",
        "PTS_OT6",
        "PTS_OT7",
        "PTS_OT8",
        "PTS_OT9",
        "PTS_OT10",
        "PTS",
    ]

    empty = pd.DataFrame(
        columns=[field.name for field in get_game_line_scores_schema()]
    )

    for attempt in range(1, retries + 1):
        try:
            summary = boxscoresummaryv2.BoxScoreSummaryV2(
                game_id=game_id,
                timeout=timeout,
            )
            available = list(summary.get_available_data())
            if "LineScore" not in available:
                return empty.copy()
            line_score = summary.get_data_frames()[available.index("LineScore")].copy()
            missing_cols = [c for c in line_score_fields if c not in line_score.columns]
            if missing_cols:
                raise ValueError(f"Missing expected line score columns: {missing_cols}")

            out = line_score[line_score_fields].copy()
            out = out.rename(
                columns={
                    "GAME_DATE_EST": "GAME_DATE",
                    "TEAM_ABBREVIATION": "TEAM_ABBR",
                }
            )
            out["GAME_DATE"] = pd.to_datetime(out["GAME_DATE"], errors="coerce").dt.date
            out = out.dropna(subset=["GAME_DATE", "GAME_ID", "TEAM_ID"]).copy()
            out["TEAM_ABBR"] = out["TEAM_ABBR"].astype("string").str.upper()
            out["SEASON"] = season
            numeric_cols = ["TEAM_ID", "PTS_QTR1", "PTS_QTR2", "PTS_QTR3", "PTS_QTR4"]
            numeric_cols.extend([f"PTS_OT{idx}" for idx in range(1, 11)])
            numeric_cols.append("PTS")
            for col in numeric_cols:
                out[col] = (
                    pd.to_numeric(out[col], errors="coerce").fillna(0).astype(int)
                )
            out["INGESTED_AT_UTC"] = pd.Timestamp.now(tz="UTC")
            return out[[field.name for field in get_game_line_scores_schema()]]
        except Exception:
            if attempt == retries:
                logger.exception(
                    "Failed NBA API line score game_id=%s after %s attempts timeout=%.1fs",
                    game_id,
                    retries,
                    timeout,
                )
                return empty.copy()
            _sleep_before_nba_api_retry(
                domain="game_line_scores",
                identifier=game_id,
                attempt=attempt,
                retries=retries,
                timeout=timeout,
                retry_base_delay=retry_base_delay,
                retry_backoff_multiplier=retry_backoff_multiplier,
                retry_max_delay=retry_max_delay,
            )

    return empty.copy()


def get_all_game_line_scores(
    game_ids: Iterable[Any],
    *,
    season: str = SUPPORTED_SEASON,
    delay: float = 0.4,
    retries: int = NBA_API_RETRIES,
    timeout: float = NBA_API_TIMEOUT_SECONDS,
    retry_base_delay: float = NBA_API_RETRY_BASE_DELAY_SECONDS,
    retry_backoff_multiplier: float = NBA_API_RETRY_BACKOFF_MULTIPLIER,
    retry_max_delay: float = NBA_API_RETRY_MAX_DELAY_SECONDS,
) -> pd.DataFrame:
    """Fetch team line scores for many games with rate limiting."""
    seen: set[str] = set()
    normalized_ids: list[str] = []
    for value in game_ids:
        if value in (None, ""):
            continue
        normalized = str(value)
        if normalized in seen:
            continue
        seen.add(normalized)
        normalized_ids.append(normalized)

    all_scores = []
    for idx, game_id in enumerate(normalized_ids, start=1):
        logger.info("Fetching line score %s/%s: %s", idx, len(normalized_ids), game_id)
        line_scores = get_game_line_scores(
            game_id,
            season=season,
            retries=retries,
            timeout=timeout,
            retry_base_delay=retry_base_delay,
            retry_backoff_multiplier=retry_backoff_multiplier,
            retry_max_delay=retry_max_delay,
        )
        if not line_scores.empty:
            all_scores.append(line_scores)
        time.sleep(delay)

    if not all_scores:
        return pd.DataFrame(
            columns=[field.name for field in get_game_line_scores_schema()]
        )

    combined = pd.concat(all_scores, ignore_index=True)
    combined = combined.drop_duplicates(subset=["GAME_ID", "TEAM_ID"]).copy()
    combined = combined.sort_values(["GAME_DATE", "GAME_ID", "TEAM_ID"]).reset_index(
        drop=True
    )
    return combined[[field.name for field in get_game_line_scores_schema()]]


def get_player_reference(
    player_id: int,
    *,
    retries: int = NBA_API_RETRIES,
    delay: Optional[float] = None,
    timeout: float = NBA_API_TIMEOUT_SECONDS,
    retry_base_delay: float = NBA_API_RETRY_BASE_DELAY_SECONDS,
    retry_backoff_multiplier: float = NBA_API_RETRY_BACKOFF_MULTIPLIER,
    retry_max_delay: float = NBA_API_RETRY_MAX_DELAY_SECONDS,
) -> pd.DataFrame:
    """Get normalized player reference attributes for a single player."""
    retries = normalize_nba_api_retries(retries)
    if delay is not None:
        retry_base_delay = delay
    expected_cols = [
        "PERSON_ID",
        "FIRST_NAME",
        "LAST_NAME",
        "DISPLAY_FIRST_LAST",
        "PLAYER_SLUG",
        "BIRTHDATE",
        "SCHOOL",
        "COUNTRY",
        "LAST_AFFILIATION",
        "HEIGHT",
        "WEIGHT",
        "SEASON_EXP",
        "JERSEY",
        "POSITION",
        "ROSTERSTATUS",
        "TEAM_ID",
        "TEAM_NAME",
        "TEAM_ABBREVIATION",
        "TEAM_CODE",
        "TEAM_CITY",
        "FROM_YEAR",
        "TO_YEAR",
        "DRAFT_YEAR",
        "DRAFT_ROUND",
        "DRAFT_NUMBER",
    ]
    empty = pd.DataFrame(
        columns=[field.name for field in get_player_reference_schema()]
    )

    for attempt in range(1, retries + 1):
        try:
            info = commonplayerinfo.CommonPlayerInfo(
                player_id=player_id,
                timeout=timeout,
            )
            available = list(info.get_available_data())
            if "CommonPlayerInfo" not in available:
                return empty.copy()
            profile = info.get_data_frames()[available.index("CommonPlayerInfo")].copy()
            if profile.empty:
                return empty.copy()
            missing_cols = [c for c in expected_cols if c not in profile.columns]
            if missing_cols:
                raise ValueError(
                    f"Missing expected player reference columns: {missing_cols}"
                )

            out = profile[expected_cols].head(1).copy()
            out = out.rename(
                columns={
                    "PERSON_ID": "PLAYER_ID",
                    "DISPLAY_FIRST_LAST": "PLAYER_NAME",
                    "TEAM_ABBREVIATION": "TEAM_ABBR",
                    "ROSTERSTATUS": "ROSTER_STATUS",
                }
            )
            out["BIRTHDATE"] = pd.to_datetime(out["BIRTHDATE"], errors="coerce").dt.date
            for col in (
                "PLAYER_ID",
                "WEIGHT",
                "SEASON_EXP",
                "TEAM_ID",
                "FROM_YEAR",
                "TO_YEAR",
            ):
                out[col] = pd.to_numeric(out[col], errors="coerce").astype("Int64")
            out["PLAYER_ID"] = out["PLAYER_ID"].fillna(player_id).astype("Int64")
            out["TEAM_ABBR"] = out["TEAM_ABBR"].astype("string").str.upper()
            out["ROSTER_STATUS"] = (
                pd.to_numeric(out["ROSTER_STATUS"], errors="coerce")
                .fillna(0)
                .astype(int)
                .astype(bool)
            )
            out["INGESTED_AT_UTC"] = pd.Timestamp.now(tz="UTC")
            return out[[field.name for field in get_player_reference_schema()]]
        except Exception:
            if attempt == retries:
                logger.exception(
                    "Failed NBA API player reference player_id=%s after %s attempts timeout=%.1fs",
                    player_id,
                    retries,
                    timeout,
                )
                return empty.copy()
            _sleep_before_nba_api_retry(
                domain="player_reference",
                identifier=player_id,
                attempt=attempt,
                retries=retries,
                timeout=timeout,
                retry_base_delay=retry_base_delay,
                retry_backoff_multiplier=retry_backoff_multiplier,
                retry_max_delay=retry_max_delay,
            )

    return empty.copy()


def get_all_player_references(
    player_list: Iterable[dict],
    *,
    delay: float = 0.4,
    retries: int = NBA_API_RETRIES,
    timeout: float = NBA_API_TIMEOUT_SECONDS,
    retry_base_delay: float = NBA_API_RETRY_BASE_DELAY_SECONDS,
    retry_backoff_multiplier: float = NBA_API_RETRY_BACKOFF_MULTIPLIER,
    retry_max_delay: float = NBA_API_RETRY_MAX_DELAY_SECONDS,
) -> pd.DataFrame:
    """Fetch player reference data for many players with rate limiting."""
    all_profiles = []
    player_list = list(player_list)

    for idx, player in enumerate(player_list, start=1):
        player_id = player["id"]
        player_name = player["full_name"]
        logger.info(
            "Fetching player reference %s/%s: %s", idx, len(player_list), player_name
        )
        profile = get_player_reference(
            player_id,
            retries=retries,
            timeout=timeout,
            retry_base_delay=retry_base_delay,
            retry_backoff_multiplier=retry_backoff_multiplier,
            retry_max_delay=retry_max_delay,
        )
        if not profile.empty:
            all_profiles.append(profile)
        time.sleep(delay)

    if not all_profiles:
        return pd.DataFrame(
            columns=[field.name for field in get_player_reference_schema()]
        )

    combined = pd.concat(all_profiles, ignore_index=True)
    combined = combined.drop_duplicates(subset=["PLAYER_ID"]).copy()
    combined = combined.sort_values(["PLAYER_ID"]).reset_index(drop=True)
    return combined[[field.name for field in get_player_reference_schema()]]


def upload_df_to_gcs(
    df: pd.DataFrame,
    project_id: str,
    bucket_name: str,
    destination_blob_name: str,
    *,
    if_generation_match: int | None = None,
) -> str:
    """Upload a DataFrame as CSV to Google Cloud Storage and return gs:// URI."""
    if df.empty:
        raise ValueError(
            f"Refusing to upload empty DataFrame to {destination_blob_name}"
        )

    gcs_client = storage.Client(project=project_id)
    bucket = gcs_client.bucket(bucket_name)
    blob = bucket.blob(destination_blob_name)

    csv_data = df.to_csv(index=False)
    upload_kwargs: Dict[str, Any] = {}
    if if_generation_match is not None:
        upload_kwargs["if_generation_match"] = if_generation_match
    blob.upload_from_string(csv_data, content_type="text/csv", **upload_kwargs)
    uri = f"gs://{bucket_name}/{destination_blob_name}"
    logger.info("Uploaded %s rows to %s", len(df), uri)
    return uri


def get_game_logs_schema() -> List[bigquery.SchemaField]:
    """Return the BigQuery schema for game logs."""
    return [
        bigquery.SchemaField("GAME_ID", "STRING"),
        bigquery.SchemaField("GAME_DATE", "DATE"),
        bigquery.SchemaField("MATCHUP", "STRING"),
        bigquery.SchemaField("WL", "STRING"),
        bigquery.SchemaField("MIN", "FLOAT"),
        bigquery.SchemaField("FGM", "FLOAT"),
        bigquery.SchemaField("FGA", "FLOAT"),
        bigquery.SchemaField("FG_PCT", "FLOAT"),
        bigquery.SchemaField("FG3M", "FLOAT"),
        bigquery.SchemaField("FG3A", "FLOAT"),
        bigquery.SchemaField("FG3_PCT", "FLOAT"),
        bigquery.SchemaField("FTM", "FLOAT"),
        bigquery.SchemaField("FTA", "FLOAT"),
        bigquery.SchemaField("FT_PCT", "FLOAT"),
        bigquery.SchemaField("OREB", "FLOAT"),
        bigquery.SchemaField("DREB", "FLOAT"),
        bigquery.SchemaField("PTS", "INTEGER"),
        bigquery.SchemaField("REB", "INTEGER"),
        bigquery.SchemaField("AST", "INTEGER"),
        bigquery.SchemaField("STL", "INTEGER"),
        bigquery.SchemaField("BLK", "INTEGER"),
        bigquery.SchemaField("TOV", "INTEGER"),
        bigquery.SchemaField("PF", "INTEGER"),
        bigquery.SchemaField("PLUS_MINUS", "FLOAT"),
        bigquery.SchemaField("SEASON", "STRING"),
        bigquery.SchemaField("INGESTED_AT_UTC", "TIMESTAMP"),
        bigquery.SchemaField("PLAYER_ID", "INTEGER"),
        bigquery.SchemaField("PLAYER_NAME", "STRING"),
    ]


def get_game_line_scores_schema() -> List[bigquery.SchemaField]:
    """Return the BigQuery schema for team line scores."""
    return [
        bigquery.SchemaField("GAME_DATE", "DATE"),
        bigquery.SchemaField("GAME_ID", "STRING"),
        bigquery.SchemaField("SEASON", "STRING"),
        bigquery.SchemaField("TEAM_ID", "INTEGER"),
        bigquery.SchemaField("TEAM_ABBR", "STRING"),
        bigquery.SchemaField("TEAM_CITY_NAME", "STRING"),
        bigquery.SchemaField("TEAM_NICKNAME", "STRING"),
        bigquery.SchemaField("TEAM_WINS_LOSSES", "STRING"),
        bigquery.SchemaField("PTS_QTR1", "INTEGER"),
        bigquery.SchemaField("PTS_QTR2", "INTEGER"),
        bigquery.SchemaField("PTS_QTR3", "INTEGER"),
        bigquery.SchemaField("PTS_QTR4", "INTEGER"),
        bigquery.SchemaField("PTS_OT1", "INTEGER"),
        bigquery.SchemaField("PTS_OT2", "INTEGER"),
        bigquery.SchemaField("PTS_OT3", "INTEGER"),
        bigquery.SchemaField("PTS_OT4", "INTEGER"),
        bigquery.SchemaField("PTS_OT5", "INTEGER"),
        bigquery.SchemaField("PTS_OT6", "INTEGER"),
        bigquery.SchemaField("PTS_OT7", "INTEGER"),
        bigquery.SchemaField("PTS_OT8", "INTEGER"),
        bigquery.SchemaField("PTS_OT9", "INTEGER"),
        bigquery.SchemaField("PTS_OT10", "INTEGER"),
        bigquery.SchemaField("PTS", "INTEGER"),
        bigquery.SchemaField("INGESTED_AT_UTC", "TIMESTAMP"),
    ]


def get_player_reference_schema() -> List[bigquery.SchemaField]:
    """Return the BigQuery schema for player reference data."""
    return [
        bigquery.SchemaField("PLAYER_ID", "INTEGER"),
        bigquery.SchemaField("FIRST_NAME", "STRING"),
        bigquery.SchemaField("LAST_NAME", "STRING"),
        bigquery.SchemaField("PLAYER_NAME", "STRING"),
        bigquery.SchemaField("PLAYER_SLUG", "STRING"),
        bigquery.SchemaField("BIRTHDATE", "DATE"),
        bigquery.SchemaField("SCHOOL", "STRING"),
        bigquery.SchemaField("COUNTRY", "STRING"),
        bigquery.SchemaField("LAST_AFFILIATION", "STRING"),
        bigquery.SchemaField("HEIGHT", "STRING"),
        bigquery.SchemaField("WEIGHT", "INTEGER"),
        bigquery.SchemaField("SEASON_EXP", "INTEGER"),
        bigquery.SchemaField("JERSEY", "STRING"),
        bigquery.SchemaField("POSITION", "STRING"),
        bigquery.SchemaField("ROSTER_STATUS", "BOOLEAN"),
        bigquery.SchemaField("TEAM_ID", "INTEGER"),
        bigquery.SchemaField("TEAM_NAME", "STRING"),
        bigquery.SchemaField("TEAM_ABBR", "STRING"),
        bigquery.SchemaField("TEAM_CODE", "STRING"),
        bigquery.SchemaField("TEAM_CITY", "STRING"),
        bigquery.SchemaField("FROM_YEAR", "INTEGER"),
        bigquery.SchemaField("TO_YEAR", "INTEGER"),
        bigquery.SchemaField("DRAFT_YEAR", "STRING"),
        bigquery.SchemaField("DRAFT_ROUND", "STRING"),
        bigquery.SchemaField("DRAFT_NUMBER", "STRING"),
        bigquery.SchemaField("INGESTED_AT_UTC", "TIMESTAMP"),
    ]


def get_injury_report_schema() -> List[bigquery.SchemaField]:
    """Return the BigQuery schema for official NBA injury report rows."""
    return [
        bigquery.SchemaField("REPORT_DATE", "DATE"),
        bigquery.SchemaField("REPORT_TIME_ET", "STRING"),
        bigquery.SchemaField("REPORT_TIMESTAMP_UTC", "TIMESTAMP"),
        bigquery.SchemaField("GAME_DATE", "DATE"),
        bigquery.SchemaField("GAME_TIME_ET", "STRING"),
        bigquery.SchemaField("MATCHUP", "STRING"),
        bigquery.SchemaField("SEASON", "STRING"),
        bigquery.SchemaField("TEAM_ABBR", "STRING"),
        bigquery.SchemaField("TEAM_NAME", "STRING"),
        bigquery.SchemaField("PLAYER_ID", "INTEGER"),
        bigquery.SchemaField("PLAYER_NAME", "STRING"),
        bigquery.SchemaField("PLAYER_NAME_SOURCE", "STRING"),
        bigquery.SchemaField("INJURY_STATUS", "STRING"),
        bigquery.SchemaField("REASON", "STRING"),
        bigquery.SchemaField("SOURCE_URL", "STRING"),
        bigquery.SchemaField("SOURCE_SYSTEM", "STRING"),
        bigquery.SchemaField("INGESTED_AT_UTC", "TIMESTAMP"),
    ]


INJURY_REPORT_STATUSES = ("Available", "Probable", "Questionable", "Doubtful", "Out")
INJURY_REPORT_STATUS_PATTERN = re.compile(
    r"\s(" + "|".join(INJURY_REPORT_STATUSES) + r")\b", re.IGNORECASE
)
_INJURY_DATE_TIME_MATCHUP_RE = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(\d{1,2}:\d{2}\s*\([A-Za-z]{2,}\))\s+"
    r"([A-Z]{2,3}@[A-Z]{2,3})\s+(.+)$"
)
_INJURY_TIME_MATCHUP_RE = re.compile(
    r"^(\d{1,2}:\d{2}\s*\([A-Za-z]{2,}\))\s+" r"([A-Z]{2,3}@[A-Z]{2,3})\s+(.+)$"
)
_INJURY_DATE_TOKEN_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
_INJURY_TIME_TOKEN_RE = re.compile(r"^\d{1,2}:\d{2}$")
_INJURY_MATCHUP_TOKEN_RE = re.compile(r"^[A-Z]{2,3}@[A-Z]{2,3}$")
_INJURY_REPORT_TOKEN_HEADER_WORDS = {
    "Injury",
    "Report:",
    "Page",
    "of",
    "Game",
    "Date",
    "Time",
    "Matchup",
    "Team",
    "Player",
    "Name",
    "Current",
    "Status",
    "Reason",
}


def normalize_player_name_key(name: Any) -> str:
    """Normalize a public NBA player name for deterministic local matching."""
    if name is None:
        return ""
    ascii_name = unicodedata.normalize("NFKD", str(name))
    ascii_name = "".join(
        character for character in ascii_name if not unicodedata.combining(character)
    )
    normalized = re.sub(r"[^a-z0-9]+", " ", ascii_name.lower()).strip()
    return re.sub(r"\s+", " ", normalized)


def normalize_official_player_name(name: Any) -> str:
    """Convert official injury report names from 'Last, First' when possible."""
    value = str(name or "").strip()
    if "," not in value:
        return re.sub(r"\s+", " ", value)
    last, first = [part.strip() for part in value.split(",", 1)]
    return re.sub(r"\s+", " ", f"{first} {last}".strip())


def build_player_id_lookup(
    player_rows: Optional[Iterable[dict]] = None,
) -> dict[str, int]:
    """Build a local lookup from NBA static player names to NBA player IDs."""
    source_rows = (
        list(player_rows) if player_rows is not None else players.get_players()
    )
    lookup: dict[str, int] = {}
    for player_row in source_rows:
        player_id = player_row.get("id") or player_row.get("PERSON_ID")
        player_name = (
            player_row.get("full_name")
            or player_row.get("DISPLAY_FIRST_LAST")
            or player_row.get("PLAYER_NAME")
        )
        key = normalize_player_name_key(player_name)
        if key and player_id is not None:
            lookup[key] = int(player_id)
    return lookup


def normalize_injury_report_status(status: Any) -> str:
    """Return a canonical NBA injury report status."""
    value = str(status or "").strip().lower()
    for allowed in INJURY_REPORT_STATUSES:
        if value == allowed.lower():
            return allowed
    return str(status or "").strip()


def normalize_injury_report_time_et(value: Any) -> str:
    """Normalize configured report times to the official URL token format."""
    raw = str(value or "").strip().upper().replace(".", "")
    if not raw:
        return DEFAULT_INJURY_REPORT_TIME_ET
    raw = raw.replace(" ", "")
    if re.fullmatch(r"\d{2}_\d{2}(AM|PM)", raw):
        return raw
    match = re.fullmatch(r"(\d{1,2}):?(\d{2})(AM|PM)", raw)
    if match:
        hour, minute, suffix = match.groups()
        return f"{int(hour):02d}_{minute}{suffix}"
    match = re.fullmatch(r"(\d{1,2})_(\d{2})(AM|PM)", raw)
    if match:
        hour, minute, suffix = match.groups()
        return f"{int(hour):02d}_{minute}{suffix}"
    raise ValueError(f"Unsupported NBA injury report time: {value!r}")


def build_official_injury_report_url(report_date: Any, report_time_et: Any) -> str:
    """Build the official NBA injury report PDF URL for one report timestamp."""
    report_day = coerce_to_date(report_date)
    if report_day is None:
        raise ValueError(f"Invalid injury report date: {report_date!r}")
    time_token = normalize_injury_report_time_et(report_time_et)
    return (
        f"{OFFICIAL_INJURY_REPORT_BASE_URL}/"
        f"Injury-Report_{report_day.isoformat()}_{time_token}.pdf"
    )


def injury_report_timestamp_utc(report_date: Any, report_time_et: Any) -> datetime:
    """Convert an official report date/time token from ET to UTC."""
    report_day = coerce_to_date(report_date)
    if report_day is None:
        raise ValueError(f"Invalid injury report date: {report_date!r}")
    time_token = normalize_injury_report_time_et(report_time_et)
    local_dt = datetime.strptime(
        f"{report_day.isoformat()} {time_token}", "%Y-%m-%d %I_%M%p"
    ).replace(tzinfo=ZoneInfo("America/New_York"))
    return local_dt.astimezone(ZoneInfo("UTC"))


def build_injury_report_candidates(
    *,
    start_date: Any,
    end_date: Any,
    report_times_et: Iterable[Any],
    max_reports: int,
) -> list[dict[str, Any]]:
    """Build a bounded list of official injury report PDFs to fetch."""
    start = coerce_to_date(start_date)
    end = coerce_to_date(end_date)
    if start is None or end is None:
        raise ValueError("start_date and end_date are required for injury reports")
    if end < start:
        return []

    normalized_times = [
        normalize_injury_report_time_et(time) for time in report_times_et
    ]
    if not normalized_times:
        normalized_times = [DEFAULT_INJURY_REPORT_TIME_ET]

    candidates: list[dict[str, Any]] = []
    current = start
    while current <= end:
        for report_time in normalized_times:
            candidates.append(
                {
                    "report_date": current,
                    "report_time_et": report_time,
                    "source_url": build_official_injury_report_url(
                        current, report_time
                    ),
                }
            )
        current += timedelta(days=1)

    limit = max(int(max_reports), 0)
    if limit and len(candidates) > limit:
        return candidates[-limit:]
    return candidates


def extract_text_from_injury_report_pdf(pdf_content: bytes) -> str:
    """Extract text from an official NBA injury report PDF."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError(
            "pypdf is required to parse NBA injury report PDFs. "
            "Install project requirements before enabling injury ingestion."
        ) from exc

    reader = PdfReader(BytesIO(pdf_content))
    page_text = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(page_text)


def _split_injury_report_team_prefix(value: str) -> tuple[Optional[str], str]:
    value_upper = value.upper()
    for team_name in sorted(NBA_TEAM_NAME_TO_ABBR, key=len, reverse=True):
        if value_upper == team_name:
            return NBA_TEAM_NAME_CANONICAL.get(team_name, team_name.title()), ""
        if value_upper.startswith(f"{team_name} "):
            return (
                NBA_TEAM_NAME_CANONICAL.get(team_name, team_name.title()),
                value[len(team_name) :].strip(),
            )
    return None, value


def _consume_injury_report_team_name(
    tokens: list[str], start_index: int
) -> tuple[Optional[str], int]:
    for team_name in sorted(
        NBA_TEAM_NAME_TO_ABBR, key=lambda value: len(value.split()), reverse=True
    ):
        team_tokens = team_name.split()
        end_index = start_index + len(team_tokens)
        if [token.upper() for token in tokens[start_index:end_index]] == team_tokens:
            return (
                NBA_TEAM_NAME_CANONICAL.get(team_name, team_name.title()),
                end_index,
            )
    return None, start_index


def _is_tokenized_injury_report(lines: list[str]) -> bool:
    meaningful = [line for line in lines if line]
    if not meaningful:
        return False
    sample = meaningful[:80]
    single_token_count = sum(1 for line in sample if " " not in line)
    return single_token_count / len(sample) >= 0.8 and any(
        _INJURY_DATE_TOKEN_RE.match(line) for line in meaningful
    )


def _is_injury_game_context_start(tokens: list[str], index: int) -> bool:
    if index >= len(tokens):
        return False
    if _INJURY_DATE_TOKEN_RE.match(tokens[index]):
        return (
            index + 3 < len(tokens)
            and _INJURY_TIME_TOKEN_RE.match(tokens[index + 1]) is not None
            and tokens[index + 2].startswith("(")
            and _INJURY_MATCHUP_TOKEN_RE.match(tokens[index + 3].upper()) is not None
        )
    return (
        index + 2 < len(tokens)
        and _INJURY_TIME_TOKEN_RE.match(tokens[index]) is not None
        and tokens[index + 1].startswith("(")
        and _INJURY_MATCHUP_TOKEN_RE.match(tokens[index + 2].upper()) is not None
    )


def _is_injury_status_token(token: Any) -> bool:
    value = str(token or "").strip().lower()
    return any(value == allowed.lower() for allowed in INJURY_REPORT_STATUSES)


def _looks_like_injury_player_start(
    tokens: list[str],
    index: int,
    *,
    player_lookup: Optional[dict[str, int]] = None,
) -> bool:
    if index >= len(tokens):
        return False
    for status_index in range(index, min(index + 7, len(tokens))):
        if _is_injury_game_context_start(tokens, status_index):
            return False
        team_name, _ = _consume_injury_report_team_name(tokens, status_index)
        if team_name:
            return False
        if _is_injury_status_token(tokens[status_index]):
            player_source = " ".join(tokens[index:status_index]).strip()
            if player_lookup is not None:
                player_key = normalize_player_name_key(
                    normalize_official_player_name(player_source)
                )
                if player_key in player_lookup:
                    return True
            comma_offsets = [
                offset
                for offset, token in enumerate(tokens[index:status_index])
                if token.endswith(",")
            ]
            return bool(comma_offsets and min(comma_offsets) == 0)
    return False


def _normalize_injury_report_lines(
    text: str,
    *,
    player_lookup: Optional[dict[str, int]] = None,
) -> list[str]:
    lines = [
        re.sub(r"\s+", " ", raw_line.strip())
        for raw_line in str(text or "").splitlines()
    ]
    lines = [line for line in lines if line]
    if not _is_tokenized_injury_report(lines):
        return lines

    tokens = lines
    logical_lines: list[str] = []
    context: dict[str, Any] = {
        "game_date": None,
        "game_time_et": "",
        "matchup": "",
        "team_name": "",
    }
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _INJURY_REPORT_TOKEN_HEADER_WORDS or re.match(r"^\d+$", token):
            index += 1
            continue

        if _INJURY_DATE_TOKEN_RE.match(token) and index + 3 < len(tokens):
            context["game_date"] = coerce_to_date(token)
            context["game_time_et"] = f"{tokens[index + 1]} {tokens[index + 2]}"
            context["matchup"] = tokens[index + 3].upper()
            index += 4
        elif (
            _INJURY_TIME_TOKEN_RE.match(token)
            and index + 2 < len(tokens)
            and tokens[index + 1].startswith("(")
            and _INJURY_MATCHUP_TOKEN_RE.match(tokens[index + 2].upper())
        ):
            context["game_time_et"] = f"{tokens[index]} {tokens[index + 1]}"
            context["matchup"] = tokens[index + 2].upper()
            index += 3

        team_name, next_index = _consume_injury_report_team_name(tokens, index)
        if team_name:
            context["team_name"] = team_name
            index = next_index

        if [token.upper() for token in tokens[index : index + 3]] == [
            "NOT",
            "YET",
            "SUBMITTED",
        ]:
            index += 3
            continue

        status_index: Optional[int] = None
        for candidate_index in range(index, len(tokens)):
            if candidate_index > index and (
                _is_injury_game_context_start(tokens, candidate_index)
                or _consume_injury_report_team_name(tokens, candidate_index)[0]
            ):
                break
            if _is_injury_status_token(tokens[candidate_index]):
                status_index = candidate_index
                break
        if status_index is None or status_index == index:
            index += 1
            continue

        player_source = " ".join(tokens[index:status_index]).strip()
        status = normalize_injury_report_status(tokens[status_index])
        reason_tokens: list[str] = []
        next_index = status_index + 1
        while next_index < len(tokens):
            if (
                _is_injury_game_context_start(tokens, next_index)
                or _consume_injury_report_team_name(tokens, next_index)[0]
                or _looks_like_injury_player_start(
                    tokens, next_index, player_lookup=player_lookup
                )
            ):
                break
            reason_tokens.append(tokens[next_index])
            next_index += 1

        if all(
            [
                context["game_date"],
                context["game_time_et"],
                context["matchup"],
                context["team_name"],
                player_source,
                status,
            ]
        ):
            logical_lines.append(
                " ".join(
                    [
                        context["game_date"].strftime("%m/%d/%Y"),
                        context["game_time_et"],
                        context["matchup"],
                        context["team_name"],
                        player_source,
                        status,
                        " ".join(reason_tokens).strip(),
                    ]
                ).strip()
            )
        index = max(next_index, status_index + 1)

    return logical_lines


def parse_injury_report_text(
    text: str,
    *,
    report_date: Any,
    report_time_et: Any,
    source_url: str,
    season: str = SUPPORTED_SEASON,
    player_lookup: Optional[dict[str, int]] = None,
    ingested_at_utc: Any = None,
) -> pd.DataFrame:
    """Parse official NBA injury report PDF text into normalized rows."""
    lookup = player_lookup if player_lookup is not None else build_player_id_lookup()
    report_day = coerce_to_date(report_date)
    report_time = normalize_injury_report_time_et(report_time_et)
    report_ts = injury_report_timestamp_utc(report_day, report_time)
    ingested_at = pd.to_datetime(
        ingested_at_utc or pd.Timestamp.now(tz="UTC"), utc=True
    )
    rows: list[dict[str, Any]] = []
    context: dict[str, Any] = {
        "game_date": None,
        "game_time_et": "",
        "matchup": "",
        "team_name": "",
        "team_abbr": "",
    }
    last_row: Optional[dict[str, Any]] = None

    for line in _normalize_injury_report_lines(text, player_lookup=lookup):
        if "Injury Report:" in line or line.startswith("Page "):
            continue
        if line.startswith("Game Date Game Time Matchup"):
            continue

        remainder = line
        date_match = _INJURY_DATE_TIME_MATCHUP_RE.match(line)
        if date_match:
            game_date_raw, game_time, matchup, remainder = date_match.groups()
            context["game_date"] = coerce_to_date(game_date_raw)
            context["game_time_et"] = game_time
            context["matchup"] = matchup.upper()
        else:
            time_match = _INJURY_TIME_MATCHUP_RE.match(line)
            if time_match:
                game_time, matchup, remainder = time_match.groups()
                context["game_time_et"] = game_time
                context["matchup"] = matchup.upper()

        team_name, remainder = _split_injury_report_team_prefix(remainder)
        if team_name:
            normalized_team_name = re.sub(r"\s+", " ", team_name).strip()
            context["team_name"] = normalized_team_name
            context["team_abbr"] = NBA_TEAM_NAME_TO_ABBR.get(
                normalized_team_name.upper(), ""
            )

        if remainder.upper() == "NOT YET SUBMITTED":
            last_row = None
            continue

        status_match = INJURY_REPORT_STATUS_PATTERN.search(remainder)
        if not status_match:
            if last_row is not None:
                last_row["REASON"] = " ".join(
                    part for part in [last_row.get("REASON", ""), remainder] if part
                ).strip()
            continue

        player_source = remainder[: status_match.start()].strip()
        status = normalize_injury_report_status(status_match.group(1))
        reason = remainder[status_match.end() :].strip()
        if not player_source or player_source.upper() == "NOT YET SUBMITTED":
            last_row = None
            continue
        if not all(
            [
                context["game_date"],
                context["matchup"],
                context["team_abbr"],
                status,
            ]
        ):
            last_row = None
            continue

        player_name = normalize_official_player_name(player_source)
        player_id = lookup.get(normalize_player_name_key(player_name))
        row = {
            "REPORT_DATE": report_day,
            "REPORT_TIME_ET": report_time,
            "REPORT_TIMESTAMP_UTC": report_ts,
            "GAME_DATE": context["game_date"],
            "GAME_TIME_ET": context["game_time_et"],
            "MATCHUP": context["matchup"],
            "SEASON": season,
            "TEAM_ABBR": context["team_abbr"],
            "TEAM_NAME": context["team_name"],
            "PLAYER_ID": player_id,
            "PLAYER_NAME": player_name,
            "PLAYER_NAME_SOURCE": player_source,
            "INJURY_STATUS": status,
            "REASON": reason,
            "SOURCE_URL": source_url,
            "SOURCE_SYSTEM": INJURY_REPORT_SOURCE_SYSTEM,
            "INGESTED_AT_UTC": ingested_at,
        }
        rows.append(row)
        last_row = row

    if not rows:
        return _empty_dataframe_for_schema(get_injury_report_schema())

    df = pd.DataFrame(rows)
    df = df.drop_duplicates(
        subset=[
            "REPORT_TIMESTAMP_UTC",
            "GAME_DATE",
            "MATCHUP",
            "TEAM_ABBR",
            "PLAYER_NAME_SOURCE",
        ]
    ).copy()
    df["PLAYER_ID"] = pd.to_numeric(df["PLAYER_ID"], errors="coerce").astype("Int64")
    return df[[field.name for field in get_injury_report_schema()]]


def fetch_official_injury_report_pdf(
    source_url: str,
    *,
    timeout: float = NBA_API_TIMEOUT_SECONDS,
    retries: int = NBA_API_RETRIES,
    retry_base_delay: float = NBA_API_RETRY_BASE_DELAY_SECONDS,
    retry_backoff_multiplier: float = NBA_API_RETRY_BACKOFF_MULTIPLIER,
    retry_max_delay: float = NBA_API_RETRY_MAX_DELAY_SECONDS,
    client: Any = None,
) -> Optional[bytes]:
    """Fetch one official NBA injury report PDF with bounded retries."""
    import httpx

    retries = normalize_nba_api_retries(retries)
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        for attempt in range(1, retries + 1):
            try:
                response = client.get(
                    source_url,
                    timeout=timeout,
                    headers=OFFICIAL_INJURY_REPORT_HEADERS,
                )
                if getattr(response, "status_code", None) == 404:
                    logger.info("Official injury report not found: %s", source_url)
                    return None
                response.raise_for_status()
                return response.content
            except Exception:
                if attempt == retries:
                    logger.exception(
                        "Failed official injury report fetch url=%s after %s attempts",
                        source_url,
                        retries,
                    )
                    return None
                _sleep_before_nba_api_retry(
                    domain="injury_report",
                    identifier=source_url,
                    attempt=attempt,
                    retries=retries,
                    timeout=timeout,
                    retry_base_delay=retry_base_delay,
                    retry_backoff_multiplier=retry_backoff_multiplier,
                    retry_max_delay=retry_max_delay,
                )
    finally:
        if owns_client:
            client.close()
    return None


def get_official_injury_report(
    candidate: dict[str, Any],
    *,
    season: str = SUPPORTED_SEASON,
    timeout: float = NBA_API_TIMEOUT_SECONDS,
    retries: int = NBA_API_RETRIES,
    retry_base_delay: float = NBA_API_RETRY_BASE_DELAY_SECONDS,
    retry_backoff_multiplier: float = NBA_API_RETRY_BACKOFF_MULTIPLIER,
    retry_max_delay: float = NBA_API_RETRY_MAX_DELAY_SECONDS,
    player_lookup: Optional[dict[str, int]] = None,
    client: Any = None,
) -> pd.DataFrame:
    """Fetch and parse one official NBA injury report candidate."""
    pdf_content = fetch_official_injury_report_pdf(
        candidate["source_url"],
        timeout=timeout,
        retries=retries,
        retry_base_delay=retry_base_delay,
        retry_backoff_multiplier=retry_backoff_multiplier,
        retry_max_delay=retry_max_delay,
        client=client,
    )
    if pdf_content is None:
        return _empty_dataframe_for_schema(get_injury_report_schema())

    text = extract_text_from_injury_report_pdf(pdf_content)
    return parse_injury_report_text(
        text,
        report_date=candidate["report_date"],
        report_time_et=candidate["report_time_et"],
        source_url=candidate["source_url"],
        season=season,
        player_lookup=player_lookup,
    )


def get_all_official_injury_reports(
    candidates: Iterable[dict[str, Any]],
    *,
    season: str = SUPPORTED_SEASON,
    delay: float = 0.25,
    timeout: float = NBA_API_TIMEOUT_SECONDS,
    retries: int = NBA_API_RETRIES,
    retry_base_delay: float = NBA_API_RETRY_BASE_DELAY_SECONDS,
    retry_backoff_multiplier: float = NBA_API_RETRY_BACKOFF_MULTIPLIER,
    retry_max_delay: float = NBA_API_RETRY_MAX_DELAY_SECONDS,
) -> pd.DataFrame:
    """Fetch a bounded set of official NBA injury reports."""
    import httpx

    player_lookup = build_player_id_lookup()
    frames: list[pd.DataFrame] = []
    candidate_list = list(candidates)
    if not candidate_list:
        return _empty_dataframe_for_schema(get_injury_report_schema())

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for candidate in candidate_list:
            frame = get_official_injury_report(
                candidate,
                season=season,
                timeout=timeout,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_backoff_multiplier=retry_backoff_multiplier,
                retry_max_delay=retry_max_delay,
                player_lookup=player_lookup,
                client=client,
            )
            if not frame.empty:
                frames.append(frame)
            time.sleep(max(delay, 0))

    if not frames:
        return _empty_dataframe_for_schema(get_injury_report_schema())

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(
        subset=[
            "REPORT_TIMESTAMP_UTC",
            "GAME_DATE",
            "MATCHUP",
            "TEAM_ABBR",
            "PLAYER_NAME_SOURCE",
        ]
    ).copy()
    combined = combined.sort_values(
        ["REPORT_TIMESTAMP_UTC", "GAME_DATE", "MATCHUP", "TEAM_ABBR", "PLAYER_NAME"],
        ascending=[True, True, True, True, True],
    )
    return combined[[field.name for field in get_injury_report_schema()]].reset_index(
        drop=True
    )


def _empty_dataframe_for_schema(schema: List[bigquery.SchemaField]) -> pd.DataFrame:
    return pd.DataFrame(columns=[field.name for field in schema])


def parse_matchup_context(matchup: Any) -> Optional[Dict[str, str]]:
    """Parse NBA API matchup strings such as 'BOS vs. NYK' or 'LAL @ PHX'."""
    if not isinstance(matchup, str):
        return None
    match = re.match(
        r"^\s*([A-Z]{2,3})\s+(VS\.|@)\s+([A-Z]{2,3})\s*$",
        matchup.strip().upper(),
    )
    if not match:
        return None
    team_abbr, marker, opponent_abbr = match.groups()
    return {
        "team_abbr": team_abbr,
        "opponent_abbr": opponent_abbr,
        "home_away": "HOME" if marker == "VS." else "AWAY",
    }


def _coerce_ingested_timestamp(value: Any) -> pd.Timestamp:
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(parsed):
        return pd.Timestamp.now(tz="UTC")
    return parsed


def _normalize_game_log_columns(game_logs: pd.DataFrame) -> pd.DataFrame:
    working = game_logs.copy()
    working.columns = [str(column).upper() for column in working.columns]
    return working


def derive_game_line_scores_from_game_logs(
    game_logs: pd.DataFrame,
    *,
    season: str = SUPPORTED_SEASON,
) -> pd.DataFrame:
    """Derive minimum team line-score rows from raw game logs."""
    if game_logs.empty:
        return _empty_dataframe_for_schema(get_game_line_scores_schema())

    working = _normalize_game_log_columns(game_logs)
    required = {"GAME_DATE", "GAME_ID", "MATCHUP", "PTS"}
    if not required.issubset(set(working.columns)):
        return _empty_dataframe_for_schema(get_game_line_scores_schema())

    totals: Dict[Tuple[date, str, str, str], Dict[str, Any]] = {}
    for row in working.to_dict("records"):
        row_season = str(row.get("SEASON") or season)
        if row_season != season:
            continue
        context = parse_matchup_context(row.get("MATCHUP"))
        game_date = coerce_to_date(row.get("GAME_DATE"))
        game_id = str(row.get("GAME_ID") or "").strip()
        if not context or game_date is None or not game_id:
            continue
        team = NBA_TEAM_LOOKUP_BY_ABBR.get(context["team_abbr"])
        if not team:
            continue
        pts = pd.to_numeric(row.get("PTS"), errors="coerce")
        key = (game_date, game_id, row_season, context["team_abbr"])
        if key not in totals:
            totals[key] = {
                "GAME_DATE": game_date,
                "GAME_ID": game_id,
                "SEASON": row_season,
                "TEAM_ID": team["team_id"],
                "TEAM_ABBR": team["team_abbr"],
                "TEAM_CITY_NAME": team["team_city_name"],
                "TEAM_NICKNAME": team["team_nickname"],
                "TEAM_WINS_LOSSES": None,
                "PTS_QTR1": 0,
                "PTS_QTR2": 0,
                "PTS_QTR3": 0,
                "PTS_QTR4": 0,
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
                "PTS": 0,
                "INGESTED_AT_UTC": _coerce_ingested_timestamp(
                    row.get("INGESTED_AT_UTC")
                ),
            }
        if not pd.isna(pts):
            totals[key]["PTS"] += int(pts)
        totals[key]["INGESTED_AT_UTC"] = max(
            totals[key]["INGESTED_AT_UTC"],
            _coerce_ingested_timestamp(row.get("INGESTED_AT_UTC")),
        )

    if not totals:
        return _empty_dataframe_for_schema(get_game_line_scores_schema())

    columns = [field.name for field in get_game_line_scores_schema()]
    result = pd.DataFrame(totals.values())
    result = result.sort_values(["GAME_DATE", "GAME_ID", "TEAM_ID"]).reset_index(
        drop=True
    )
    return result[columns]


def derive_schedule_from_game_logs(
    game_logs: pd.DataFrame,
    *,
    season: str = SUPPORTED_SEASON,
) -> pd.DataFrame:
    """Derive observed team schedule rows from raw game logs."""
    if game_logs.empty:
        return _empty_dataframe_for_schema(get_schedule_schema())

    working = _normalize_game_log_columns(game_logs)
    required = {"GAME_DATE", "GAME_ID", "MATCHUP"}
    if not required.issubset(set(working.columns)):
        return _empty_dataframe_for_schema(get_schedule_schema())

    by_team_game: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in working.to_dict("records"):
        row_season = str(row.get("SEASON") or season)
        if row_season != season:
            continue
        context = parse_matchup_context(row.get("MATCHUP"))
        game_date = coerce_to_date(row.get("GAME_DATE"))
        game_id = str(row.get("GAME_ID") or "").strip()
        if not context or game_date is None or not game_id:
            continue
        if context["team_abbr"] not in NBA_TEAM_LOOKUP_BY_ABBR:
            continue
        key = (game_id, context["team_abbr"])
        ingested_at = _coerce_ingested_timestamp(row.get("INGESTED_AT_UTC"))
        if key not in by_team_game:
            by_team_game[key] = {
                "SCHEDULE_DATE": game_date,
                "GAME_ID": game_id,
                "SEASON": row_season,
                "TEAM_ABBR": context["team_abbr"],
                "OPPONENT_ABBR": context["opponent_abbr"],
                "HOME_AWAY": context["home_away"],
                "IS_BACK_TO_BACK": False,
                "GAME_STATUS": "BOOTSTRAPPED_FROM_GAME_LOGS",
                "SOURCE_UPDATED_AT_UTC": ingested_at,
                "INGESTED_AT_UTC": ingested_at,
            }
            continue
        by_team_game[key]["SOURCE_UPDATED_AT_UTC"] = max(
            by_team_game[key]["SOURCE_UPDATED_AT_UTC"], ingested_at
        )
        by_team_game[key]["INGESTED_AT_UTC"] = max(
            by_team_game[key]["INGESTED_AT_UTC"], ingested_at
        )

    if not by_team_game:
        return _empty_dataframe_for_schema(get_schedule_schema())

    columns = [field.name for field in get_schedule_schema()]
    result = pd.DataFrame(by_team_game.values())
    result = result.sort_values(["TEAM_ABBR", "SCHEDULE_DATE", "GAME_ID"]).reset_index(
        drop=True
    )
    schedule_dates = pd.to_datetime(result["SCHEDULE_DATE"], errors="coerce")
    previous_dates = schedule_dates.groupby(result["TEAM_ABBR"]).shift(1)
    result["IS_BACK_TO_BACK"] = previous_dates.notna() & (
        (schedule_dates - previous_dates).dt.days == 1
    )
    result = result.sort_values(["SCHEDULE_DATE", "GAME_ID", "TEAM_ABBR"]).reset_index(
        drop=True
    )
    return result[columns]


def _split_player_name(player_name: str) -> Tuple[Optional[str], Optional[str]]:
    parts = player_name.strip().split(maxsplit=1)
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], parts[1]


def _player_slug(player_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", player_name.lower()).strip("-")
    return slug


def derive_player_reference_from_game_logs(
    game_logs: pd.DataFrame,
    *,
    season: str = SUPPORTED_SEASON,
) -> pd.DataFrame:
    """Derive minimum player reference rows from raw game logs."""
    if game_logs.empty:
        return _empty_dataframe_for_schema(get_player_reference_schema())

    working = _normalize_game_log_columns(game_logs)
    required = {"PLAYER_ID", "PLAYER_NAME", "GAME_DATE", "MATCHUP"}
    if not required.issubset(set(working.columns)):
        return _empty_dataframe_for_schema(get_player_reference_schema())

    latest_by_player: Dict[int, Dict[str, Any]] = {}
    for row in working.to_dict("records"):
        row_season = str(row.get("SEASON") or season)
        if row_season != season:
            continue
        context = parse_matchup_context(row.get("MATCHUP"))
        player_id = pd.to_numeric(row.get("PLAYER_ID"), errors="coerce")
        player_name = str(row.get("PLAYER_NAME") or "").strip()
        game_date = coerce_to_date(row.get("GAME_DATE"))
        if (
            not context
            or pd.isna(player_id)
            or not player_name
            or game_date is None
            or context["team_abbr"] not in NBA_TEAM_LOOKUP_BY_ABBR
        ):
            continue
        player_key = int(player_id)
        ingested_at = _coerce_ingested_timestamp(row.get("INGESTED_AT_UTC"))
        candidate_sort = (game_date, ingested_at)
        current = latest_by_player.get(player_key)
        if current and current["_sort"] >= candidate_sort:
            continue
        latest_by_player[player_key] = {
            "player_id": player_key,
            "player_name": player_name,
            "team_abbr": context["team_abbr"],
            "ingested_at_utc": ingested_at,
            "_sort": candidate_sort,
        }

    if not latest_by_player:
        return _empty_dataframe_for_schema(get_player_reference_schema())

    records = []
    for player in latest_by_player.values():
        team = NBA_TEAM_LOOKUP_BY_ABBR[player["team_abbr"]]
        first_name, last_name = _split_player_name(player["player_name"])
        team_name = f"{team['team_city_name']} {team['team_nickname']}"
        records.append(
            {
                "PLAYER_ID": player["player_id"],
                "FIRST_NAME": first_name,
                "LAST_NAME": last_name,
                "PLAYER_NAME": player["player_name"],
                "PLAYER_SLUG": _player_slug(player["player_name"]),
                "BIRTHDATE": None,
                "SCHOOL": None,
                "COUNTRY": None,
                "LAST_AFFILIATION": None,
                "HEIGHT": None,
                "WEIGHT": None,
                "SEASON_EXP": None,
                "JERSEY": None,
                "POSITION": None,
                "ROSTER_STATUS": True,
                "TEAM_ID": team["team_id"],
                "TEAM_NAME": team_name,
                "TEAM_ABBR": team["team_abbr"],
                "TEAM_CODE": re.sub(r"[^a-z0-9]+", "", team["team_nickname"].lower()),
                "TEAM_CITY": team["team_city_name"],
                "FROM_YEAR": None,
                "TO_YEAR": None,
                "DRAFT_YEAR": None,
                "DRAFT_ROUND": None,
                "DRAFT_NUMBER": None,
                "INGESTED_AT_UTC": player["ingested_at_utc"],
            }
        )

    columns = [field.name for field in get_player_reference_schema()]
    result = pd.DataFrame(records).sort_values(["PLAYER_ID"]).reset_index(drop=True)
    return result[columns]


def normalize_bronze_bootstrap_mode(mode: Optional[str]) -> str:
    """Normalize and validate the bronze bootstrap mode."""
    normalized = (mode or "auto").strip().lower()
    if normalized not in {"auto", "off", "force"}:
        raise ValueError("NBA_BRONZE_BOOTSTRAP_MODE must be one of: auto, off, force")
    return normalized


def should_bootstrap_bronze_table(
    mode: Optional[str],
    *,
    raw_game_logs_rows: Optional[int],
    target_rows: Optional[int],
) -> bool:
    """Return whether a derived bronze table should be bootstrapped."""
    normalized = normalize_bronze_bootstrap_mode(mode)
    if normalized == "off":
        return False
    if not raw_game_logs_rows:
        return False
    if normalized == "force":
        return True
    return target_rows in (None, 0)


_BIGQUERY_TABLE_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_-]*\.[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$"
)


def quote_bigquery_table_id(table_id: str) -> str:
    """Return a backticked BigQuery table identifier after conservative validation."""
    if not _BIGQUERY_TABLE_ID_RE.fullmatch(str(table_id or "")):
        raise ValueError(f"Unsafe BigQuery table identifier: {table_id!r}")
    return f"`{table_id}`"


def get_table_row_count(
    bq_client: bigquery.Client,
    table_id: str,
) -> Optional[int]:
    """Return a BigQuery table row count, or None when the table is absent."""
    try:
        row = (
            bq_client.query(f"SELECT COUNT(*) AS c FROM `{table_id}`")
            .to_dataframe()
            .iloc[0]
        )
    except NotFound:
        return None
    return int(row["c"])


def _sql_literal(value: Any) -> str:
    return str(value).replace("'", "''")


def _team_lookup_sql() -> str:
    return "\nUNION ALL\n".join(
        (
            f"SELECT '{_sql_literal(team['team_abbr'])}' AS team_abbr, "
            f"{int(team['team_id'])} AS team_id, "
            f"'{_sql_literal(team['team_city_name'])}' AS team_city_name, "
            f"'{_sql_literal(team['team_nickname'])}' AS team_nickname"
        )
        for team in NBA_TEAM_LOOKUP
    )


def _count_query_rows(
    bq_client: bigquery.Client,
    table_id: str,
) -> int:
    count = get_table_row_count(bq_client, table_id)
    return int(count or 0)


def create_bronze_bootstrap_line_scores_staging(
    bq_client: bigquery.Client,
    *,
    raw_game_logs_table: str,
    staging_table: str,
    season: str = SUPPORTED_SEASON,
) -> int:
    """Create staging line scores derived from raw game logs."""
    query = f"""
    CREATE OR REPLACE TABLE `{staging_table}` AS
    WITH team_lookup AS (
      {_team_lookup_sql()}
    ),
    parsed AS (
      SELECT
        DATE(game_date) AS game_date,
        NULLIF(TRIM(CAST(game_id AS STRING)), '') AS game_id,
        CAST(season AS STRING) AS season,
        UPPER(REGEXP_EXTRACT(CAST(matchup AS STRING), r'^([A-Z]{{2,3}})')) AS team_abbr,
        SAFE_CAST(pts AS INT64) AS pts,
        CAST(ingested_at_utc AS TIMESTAMP) AS ingested_at_utc
      FROM `{raw_game_logs_table}`
      WHERE CAST(season AS STRING) = @season
        AND game_date IS NOT NULL
        AND matchup IS NOT NULL
    ),
    team_totals AS (
      SELECT
        game_date,
        game_id,
        season,
        team_abbr,
        SUM(COALESCE(pts, 0)) AS pts,
        MAX(ingested_at_utc) AS source_updated_at_utc
      FROM parsed
      WHERE game_id IS NOT NULL
        AND team_abbr IS NOT NULL
      GROUP BY 1, 2, 3, 4
    )
    SELECT
      t.game_date AS game_date,
      t.game_id AS game_id,
      t.season AS season,
      l.team_id AS team_id,
      t.team_abbr AS team_abbr,
      l.team_city_name AS team_city_name,
      l.team_nickname AS team_nickname,
      CAST(NULL AS STRING) AS team_wins_losses,
      0 AS pts_qtr1,
      0 AS pts_qtr2,
      0 AS pts_qtr3,
      0 AS pts_qtr4,
      0 AS pts_ot1,
      0 AS pts_ot2,
      0 AS pts_ot3,
      0 AS pts_ot4,
      0 AS pts_ot5,
      0 AS pts_ot6,
      0 AS pts_ot7,
      0 AS pts_ot8,
      0 AS pts_ot9,
      0 AS pts_ot10,
      CAST(t.pts AS INT64) AS pts,
      COALESCE(t.source_updated_at_utc, CURRENT_TIMESTAMP()) AS ingested_at_utc
    FROM team_totals t
    INNER JOIN team_lookup l
      ON t.team_abbr = l.team_abbr
    """
    bq_client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("season", "STRING", season),
            ]
        ),
    ).result()
    return _count_query_rows(bq_client, staging_table)


def create_bronze_bootstrap_schedule_staging(
    bq_client: bigquery.Client,
    *,
    raw_game_logs_table: str,
    staging_table: str,
    season: str = SUPPORTED_SEASON,
) -> int:
    """Create staging schedule rows derived from raw game logs."""
    query = f"""
    CREATE OR REPLACE TABLE `{staging_table}` AS
    WITH parsed AS (
      SELECT
        DATE(game_date) AS schedule_date,
        NULLIF(TRIM(CAST(game_id AS STRING)), '') AS game_id,
        CAST(season AS STRING) AS season,
        UPPER(REGEXP_EXTRACT(CAST(matchup AS STRING), r'^([A-Z]{{2,3}})')) AS team_abbr,
        UPPER(REGEXP_EXTRACT(CAST(matchup AS STRING), r'([A-Z]{{2,3}})$')) AS opponent_abbr,
        CASE
          WHEN REGEXP_CONTAINS(CAST(matchup AS STRING), r'\\s+vs\\.\\s+') THEN 'HOME'
          WHEN REGEXP_CONTAINS(CAST(matchup AS STRING), r'\\s+@\\s+') THEN 'AWAY'
          ELSE NULL
        END AS home_away,
        CAST(ingested_at_utc AS TIMESTAMP) AS ingested_at_utc
      FROM `{raw_game_logs_table}`
      WHERE CAST(season AS STRING) = @season
        AND game_date IS NOT NULL
        AND matchup IS NOT NULL
    ),
    team_games AS (
      SELECT
        schedule_date,
        game_id,
        season,
        team_abbr,
        opponent_abbr,
        home_away,
        MAX(ingested_at_utc) AS source_updated_at_utc
      FROM parsed
      WHERE game_id IS NOT NULL
        AND team_abbr IS NOT NULL
        AND opponent_abbr IS NOT NULL
        AND home_away IS NOT NULL
      GROUP BY 1, 2, 3, 4, 5, 6
    ),
    with_previous AS (
      SELECT
        *,
        LAG(schedule_date) OVER (
          PARTITION BY team_abbr
          ORDER BY schedule_date, game_id
        ) AS previous_schedule_date
      FROM team_games
    )
    SELECT
      schedule_date,
      game_id,
      season,
      team_abbr,
      opponent_abbr,
      home_away,
      previous_schedule_date IS NOT NULL
        AND DATE_DIFF(schedule_date, previous_schedule_date, DAY) = 1
        AS is_back_to_back,
      'BOOTSTRAPPED_FROM_GAME_LOGS' AS game_status,
      COALESCE(source_updated_at_utc, CURRENT_TIMESTAMP()) AS source_updated_at_utc,
      CURRENT_TIMESTAMP() AS ingested_at_utc
    FROM with_previous
    """
    bq_client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("season", "STRING", season),
            ]
        ),
    ).result()
    return _count_query_rows(bq_client, staging_table)


def create_bronze_bootstrap_player_reference_staging(
    bq_client: bigquery.Client,
    *,
    raw_game_logs_table: str,
    staging_table: str,
    season: str = SUPPORTED_SEASON,
) -> int:
    """Create staging player-reference rows derived from raw game logs."""
    query = f"""
    CREATE OR REPLACE TABLE `{staging_table}` AS
    WITH team_lookup AS (
      {_team_lookup_sql()}
    ),
    parsed AS (
      SELECT
        SAFE_CAST(player_id AS INT64) AS player_id,
        NULLIF(TRIM(CAST(player_name AS STRING)), '') AS player_name,
        UPPER(REGEXP_EXTRACT(CAST(matchup AS STRING), r'^([A-Z]{{2,3}})')) AS team_abbr,
        DATE(game_date) AS game_date,
        CAST(ingested_at_utc AS TIMESTAMP) AS ingested_at_utc
      FROM `{raw_game_logs_table}`
      WHERE CAST(season AS STRING) = @season
        AND player_id IS NOT NULL
        AND player_name IS NOT NULL
        AND game_date IS NOT NULL
        AND matchup IS NOT NULL
    ),
    latest AS (
      SELECT
        ARRAY_AGG(
          STRUCT(player_id, player_name, team_abbr, game_date, ingested_at_utc)
          ORDER BY game_date DESC, ingested_at_utc DESC
          LIMIT 1
        )[OFFSET(0)] AS row
      FROM parsed
      WHERE player_id IS NOT NULL
        AND player_name IS NOT NULL
        AND team_abbr IS NOT NULL
      GROUP BY player_id
    )
    SELECT
      row.player_id AS player_id,
      CAST(NULL AS STRING) AS first_name,
      CAST(NULL AS STRING) AS last_name,
      row.player_name AS player_name,
      REGEXP_REPLACE(
        REGEXP_REPLACE(LOWER(row.player_name), r'[^a-z0-9]+', '-'),
        r'^-|-$',
        ''
      ) AS player_slug,
      CAST(NULL AS DATE) AS birthdate,
      CAST(NULL AS STRING) AS school,
      CAST(NULL AS STRING) AS country,
      CAST(NULL AS STRING) AS last_affiliation,
      CAST(NULL AS STRING) AS height,
      CAST(NULL AS INT64) AS weight,
      CAST(NULL AS INT64) AS season_exp,
      CAST(NULL AS STRING) AS jersey,
      CAST(NULL AS STRING) AS position,
      TRUE AS roster_status,
      l.team_id AS team_id,
      CONCAT(l.team_city_name, ' ', l.team_nickname) AS team_name,
      row.team_abbr AS team_abbr,
      REGEXP_REPLACE(LOWER(l.team_nickname), r'[^a-z0-9]+', '') AS team_code,
      l.team_city_name AS team_city,
      CAST(NULL AS INT64) AS from_year,
      CAST(NULL AS INT64) AS to_year,
      CAST(NULL AS STRING) AS draft_year,
      CAST(NULL AS STRING) AS draft_round,
      CAST(NULL AS STRING) AS draft_number,
      COALESCE(row.ingested_at_utc, CURRENT_TIMESTAMP()) AS ingested_at_utc
    FROM latest
    INNER JOIN team_lookup l
      ON row.team_abbr = l.team_abbr
    """
    bq_client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("season", "STRING", season),
            ]
        ),
    ).result()
    return _count_query_rows(bq_client, staging_table)


def _skipped_bootstrap_domain(
    *,
    domain: str,
    raw_table: str,
    target_rows: Optional[int],
    raw_game_logs_rows: Optional[int],
    mode: str,
    reason: str,
) -> Dict[str, Any]:
    return {
        "domain": domain,
        "raw_table": raw_table,
        "mode": mode,
        "ran": False,
        "reason": reason,
        "target_rows_before": target_rows,
        "raw_game_logs_rows": raw_game_logs_rows,
        "rows_loaded": 0,
        "rows_inserted": 0,
        "rows_updated": 0,
        "rows_unchanged": 0,
    }


def _bootstrap_domain(
    *,
    bq_client: bigquery.Client,
    domain: str,
    raw_game_logs_table: str,
    raw_table: str,
    staging_table: str,
    create_staging,
    run_dq,
    merge_table,
    season: str,
    mode: str,
    raw_game_logs_rows: Optional[int],
) -> Dict[str, Any]:
    target_rows = get_table_row_count(bq_client, raw_table)
    if not should_bootstrap_bronze_table(
        mode, raw_game_logs_rows=raw_game_logs_rows, target_rows=target_rows
    ):
        return _skipped_bootstrap_domain(
            domain=domain,
            raw_table=raw_table,
            target_rows=target_rows,
            raw_game_logs_rows=raw_game_logs_rows,
            mode=mode,
            reason="bootstrap not required",
        )

    rows_loaded = create_staging(
        bq_client,
        raw_game_logs_table=raw_game_logs_table,
        staging_table=staging_table,
        season=season,
    )
    if rows_loaded == 0:
        return _skipped_bootstrap_domain(
            domain=domain,
            raw_table=raw_table,
            target_rows=target_rows,
            raw_game_logs_rows=raw_game_logs_rows,
            mode=mode,
            reason="bootstrap staging produced zero rows",
        )

    dq_results = run_dq(bq_client, staging_table)
    merge_result = merge_table(bq_client, staging_table, raw_table)
    reconciliation = validate_merge_reconciliation(
        domain=f"{domain}_bootstrap",
        rows_loaded=rows_loaded,
        pre_count=merge_result["pre_count"],
        post_count=merge_result["post_count"],
        inserted=merge_result["inserted"],
        updated=merge_result["updated"],
    )
    return {
        "domain": domain,
        "raw_table": raw_table,
        "staging_table": staging_table,
        "mode": mode,
        "ran": True,
        "target_rows_before": target_rows,
        "target_rows_after": merge_result["post_count"],
        "raw_game_logs_rows": raw_game_logs_rows,
        "rows_loaded": rows_loaded,
        "rows_inserted": merge_result["inserted"],
        "rows_updated": merge_result["updated"],
        "rows_unchanged": reconciliation["unchanged"],
        "dq_results": dq_results,
        "reconciliation": reconciliation,
    }


def run_bronze_contract_bootstrap(
    bq_client: bigquery.Client,
    *,
    project_id: str,
    bronze_dataset: str,
    season: str = SUPPORTED_SEASON,
    mode: Optional[str] = "auto",
) -> Dict[str, Any]:
    """Bootstrap derived bronze contract tables from raw_game_logs when needed."""
    normalized_mode = normalize_bronze_bootstrap_mode(mode)
    raw_game_logs_table = f"{project_id}.{bronze_dataset}.raw_game_logs"
    raw_game_logs_rows = get_table_row_count(bq_client, raw_game_logs_table)

    domains: Dict[str, Dict[str, Any]] = {}
    if not raw_game_logs_rows:
        for domain, table_name in {
            "schedule": "raw_schedule",
            "game_line_scores": "raw_game_line_scores",
            "player_reference": "raw_player_reference",
        }.items():
            domains[domain] = _skipped_bootstrap_domain(
                domain=domain,
                raw_table=f"{project_id}.{bronze_dataset}.{table_name}",
                target_rows=None,
                raw_game_logs_rows=raw_game_logs_rows,
                mode=normalized_mode,
                reason="raw_game_logs is missing or empty",
            )
        return {
            "mode": normalized_mode,
            "raw_game_logs_table": raw_game_logs_table,
            "raw_game_logs_rows": raw_game_logs_rows,
            "domains": domains,
        }

    domains["schedule"] = _bootstrap_domain(
        bq_client=bq_client,
        domain="schedule",
        raw_game_logs_table=raw_game_logs_table,
        raw_table=f"{project_id}.{bronze_dataset}.raw_schedule",
        staging_table=f"{project_id}.{bronze_dataset}.stg_bootstrap_schedule",
        create_staging=create_bronze_bootstrap_schedule_staging,
        run_dq=lambda client, table: run_schedule_quality_checks(
            client, table, season=season
        ),
        merge_table=create_and_merge_schedule_table,
        season=season,
        mode=normalized_mode,
        raw_game_logs_rows=raw_game_logs_rows,
    )
    domains["game_line_scores"] = _bootstrap_domain(
        bq_client=bq_client,
        domain="game_line_scores",
        raw_game_logs_table=raw_game_logs_table,
        raw_table=f"{project_id}.{bronze_dataset}.raw_game_line_scores",
        staging_table=f"{project_id}.{bronze_dataset}.stg_bootstrap_game_line_scores",
        create_staging=create_bronze_bootstrap_line_scores_staging,
        run_dq=lambda client, table: run_game_line_score_quality_checks(
            client, table, season=season
        ),
        merge_table=create_and_merge_game_line_scores_table,
        season=season,
        mode=normalized_mode,
        raw_game_logs_rows=raw_game_logs_rows,
    )
    domains["player_reference"] = _bootstrap_domain(
        bq_client=bq_client,
        domain="player_reference",
        raw_game_logs_table=raw_game_logs_table,
        raw_table=f"{project_id}.{bronze_dataset}.raw_player_reference",
        staging_table=f"{project_id}.{bronze_dataset}.stg_bootstrap_player_reference",
        create_staging=create_bronze_bootstrap_player_reference_staging,
        run_dq=run_player_reference_quality_checks,
        merge_table=create_and_merge_player_reference_table,
        season=season,
        mode=normalized_mode,
        raw_game_logs_rows=raw_game_logs_rows,
    )
    return {
        "mode": normalized_mode,
        "raw_game_logs_table": raw_game_logs_table,
        "raw_game_logs_rows": raw_game_logs_rows,
        "domains": domains,
    }


def apply_bootstrap_domain_result(
    current_result: Dict[str, Any],
    bootstrap_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Add bootstrap accounting to a normal Airflow domain result."""
    updated = dict(current_result)
    updated["bronze_bootstrap"] = bootstrap_result
    if not bootstrap_result.get("ran"):
        return updated
    for source_key, target_key in (
        ("rows_loaded", "rows_loaded"),
        ("rows_inserted", "rows_inserted"),
        ("rows_updated", "rows_updated"),
        ("rows_unchanged", "rows_unchanged"),
    ):
        updated[target_key] = int(updated.get(target_key, 0) or 0) + int(
            bootstrap_result.get(source_key, 0) or 0
        )
    updated["bootstrap_dq_results"] = bootstrap_result.get("dq_results", {})
    updated["bootstrap_reconciliation"] = bootstrap_result.get("reconciliation", {})
    return updated


_BQ_TYPE_MAP = {
    "INTEGER": "INT64",
    "FLOAT": "FLOAT64",
    "BOOLEAN": "BOOL",
}
_ALLOWED_DDL_TYPES = {
    "INT64",
    "FLOAT64",
    "BOOL",
    "STRING",
    "DATE",
    "TIMESTAMP",
    "DATETIME",
    "NUMERIC",
    "BIGNUMERIC",
    "BYTES",
    "JSON",
}


def schema_field_to_sql_type(field: bigquery.SchemaField) -> str:
    """Map BigQuery schema field types to stable DDL type strings."""
    type_name = field.field_type.upper()
    mapped = _BQ_TYPE_MAP.get(type_name, type_name)
    if mapped not in _ALLOWED_DDL_TYPES:
        raise ValueError(f"Unsupported DDL type for column {field.name!r}: {mapped!r}")
    return mapped


def ensure_table_has_columns(
    bq_client: bigquery.Client,
    table_id: str,
    schema: List[bigquery.SchemaField],
) -> None:
    """Add any missing columns from the expected schema to an existing table.

    Each column is attempted independently so a single failure does not leave
    the table in a partially-migrated state silently — errors are logged and
    re-raised after all columns have been attempted.
    """
    errors = []
    for field in schema:
        alter_sql = (
            f"ALTER TABLE `{table_id}` "
            f"ADD COLUMN IF NOT EXISTS {field.name.lower()} {schema_field_to_sql_type(field)}"
        )
        try:
            bq_client.query(alter_sql).result()
        except Exception as exc:
            logger.error(
                "ensure_table_has_columns: failed to add column %s to %s: %s",
                field.name,
                table_id,
                exc,
            )
            errors.append((field.name, exc))
    if errors:
        names = ", ".join(n for n, _ in errors)
        raise RuntimeError(
            f"ensure_table_has_columns: {len(errors)} column(s) failed for {table_id}: {names}"
        )


def load_gcs_to_bigquery(
    bq_client: bigquery.Client,
    gcs_uri: str,
    table_id: str,
    schema: List[bigquery.SchemaField],
    partition_field: Optional[str] = None,
    clustering_fields: Optional[List[str]] = None,
    write_disposition: str = bigquery.WriteDisposition.WRITE_APPEND,
) -> None:
    """Load a CSV from GCS into BigQuery with optional partitioning and clustering."""
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.CSV,
        skip_leading_rows=1,
        schema=schema,
        write_disposition=write_disposition,
    )

    if partition_field:
        job_config.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field=partition_field,
        )
    if clustering_fields:
        job_config.clustering_fields = clustering_fields

    load_job = bq_client.load_table_from_uri(gcs_uri, table_id, job_config=job_config)
    load_job.result()

    table = bq_client.get_table(table_id)
    logger.info(
        "Loaded %s rows to %s (write=%s, partitioned=%s, clustered=%s)",
        table.num_rows,
        table_id,
        write_disposition,
        partition_field or "none",
        clustering_fields or "none",
    )


def run_data_quality_checks(
    bq_client: bigquery.Client,
    staging_table: str,
    *,
    season: str = SUPPORTED_SEASON,
) -> dict:
    """Run data quality checks on staging table. Raises ValueError on failure."""
    season_start, season_end = get_season_date_bounds(season)
    dq_query = f"""
    WITH base AS (
      SELECT *
      FROM `{staging_table}`
    ),
    dups AS (
      SELECT COUNT(*) AS duplicate_keys
      FROM (
        SELECT player_id, game_date, matchup, COUNT(*) AS cnt
        FROM base
        GROUP BY player_id, game_date, matchup
        HAVING COUNT(*) > 1
      )
    )
    SELECT
      (SELECT COUNT(*) FROM base) AS total_rows,
      (SELECT COUNT(*) FROM base WHERE player_id IS NULL OR game_date IS NULL OR matchup IS NULL) AS null_key_rows,
      (SELECT duplicate_keys FROM dups) AS duplicate_key_rows,
      (SELECT COUNT(*) FROM base WHERE season != @season OR season IS NULL) AS invalid_season_rows,
      (SELECT COUNT(*) FROM base WHERE game_date < @season_start OR game_date > @season_end) AS out_of_window_rows,
      (SELECT COUNT(*) FROM base WHERE wl IS NOT NULL AND upper(wl) NOT IN ('W', 'L')) AS invalid_wl_rows,
      (
        SELECT COUNT(*)
        FROM base
        WHERE (fg_pct IS NOT NULL AND (fg_pct < 0 OR fg_pct > 1))
           OR (ft_pct IS NOT NULL AND (ft_pct < 0 OR ft_pct > 1))
           OR (fg3_pct IS NOT NULL AND (fg3_pct < 0 OR fg3_pct > 1))
      ) AS invalid_pct_rows
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("season", "STRING", season),
            bigquery.ScalarQueryParameter(
                "season_start", "DATE", season_start.isoformat()
            ),
            bigquery.ScalarQueryParameter("season_end", "DATE", season_end.isoformat()),
        ]
    )
    dq = (
        bq_client.query(dq_query, job_config=job_config)
        .to_dataframe()
        .iloc[0]
        .to_dict()
    )
    logger.info("DQ results: %s", dq)

    if dq["total_rows"] == 0:
        raise ValueError("DQ failed: staging table has zero rows")
    if dq["null_key_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['null_key_rows']} rows with null business keys"
        )
    if dq["duplicate_key_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['duplicate_key_rows']} duplicate business keys"
        )
    if dq["invalid_season_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['invalid_season_rows']} rows outside season {season}"
        )
    if dq["out_of_window_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['out_of_window_rows']} rows outside date window "
            f"{season_start.isoformat()} to {season_end.isoformat()}"
        )
    if dq["invalid_wl_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['invalid_wl_rows']} rows with invalid WL values"
        )
    if dq["invalid_pct_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['invalid_pct_rows']} rows with invalid shooting percentages"
        )

    return dq


def run_game_line_score_quality_checks(
    bq_client: bigquery.Client,
    staging_table: str,
    *,
    season: str = SUPPORTED_SEASON,
) -> dict:
    """Run data quality checks on game line score staging rows."""
    dq_query = f"""
    WITH base AS (
      SELECT *
      FROM `{staging_table}`
    ),
    dups AS (
      SELECT COUNT(*) AS duplicate_keys
      FROM (
        SELECT game_id, team_id, COUNT(*) AS cnt
        FROM base
        GROUP BY game_id, team_id
        HAVING COUNT(*) > 1
      )
    )
    SELECT
      (SELECT COUNT(*) FROM base) AS total_rows,
      (SELECT COUNT(*) FROM base WHERE game_id IS NULL OR team_id IS NULL OR team_abbr IS NULL) AS null_key_rows,
      (SELECT duplicate_keys FROM dups) AS duplicate_key_rows,
      (SELECT COUNT(*) FROM base WHERE season != @season OR season IS NULL) AS invalid_season_rows,
      (SELECT COUNT(*) FROM base WHERE pts IS NULL OR pts < 0) AS invalid_points_rows
    """
    dq = (
        bq_client.query(
            dq_query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("season", "STRING", season),
                ]
            ),
        )
        .to_dataframe()
        .iloc[0]
        .to_dict()
    )
    if dq["total_rows"] == 0:
        raise ValueError("DQ failed: game line score staging table has zero rows")
    if dq["null_key_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['null_key_rows']} game line score rows with null keys"
        )
    if dq["duplicate_key_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['duplicate_key_rows']} duplicate game line score rows"
        )
    if dq["invalid_season_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['invalid_season_rows']} game line score rows outside season {season}"
        )
    if dq["invalid_points_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['invalid_points_rows']} game line score rows with invalid points"
        )
    return dq


def run_player_reference_quality_checks(
    bq_client: bigquery.Client,
    staging_table: str,
) -> dict:
    """Run data quality checks on player reference staging rows."""
    dq_query = f"""
    WITH base AS (
      SELECT *
      FROM `{staging_table}`
    ),
    dups AS (
      SELECT COUNT(*) AS duplicate_keys
      FROM (
        SELECT player_id, COUNT(*) AS cnt
        FROM base
        GROUP BY player_id
        HAVING COUNT(*) > 1
      )
    )
    SELECT
      (SELECT COUNT(*) FROM base) AS total_rows,
      (SELECT COUNT(*) FROM base WHERE player_id IS NULL) AS null_key_rows,
      (SELECT duplicate_keys FROM dups) AS duplicate_key_rows,
      (SELECT COUNT(*) FROM base WHERE player_name IS NULL OR trim(player_name) = '') AS missing_name_rows
    """
    dq = bq_client.query(dq_query).to_dataframe().iloc[0].to_dict()
    if dq["total_rows"] == 0:
        raise ValueError("DQ failed: player reference staging table has zero rows")
    if dq["null_key_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['null_key_rows']} player reference rows with null player_id"
        )
    if dq["duplicate_key_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['duplicate_key_rows']} duplicate player reference rows"
        )
    if dq["missing_name_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['missing_name_rows']} player reference rows without a player_name"
        )
    return dq


def validate_merge_reconciliation(
    *,
    domain: str,
    rows_loaded: int,
    pre_count: int,
    post_count: int,
    inserted: int,
    updated: int,
) -> Dict[str, int]:
    """Validate that merge accounting is internally consistent."""
    if rows_loaded < 0:
        raise ValueError(
            f"Reconciliation failed for {domain}: rows_loaded cannot be negative"
        )
    if any(value < 0 for value in (pre_count, post_count, inserted, updated)):
        raise ValueError(
            f"Reconciliation failed for {domain}: merge counts cannot be negative"
        )

    if inserted + updated > rows_loaded:
        raise ValueError(
            f"Reconciliation failed for {domain}: inserted+updated "
            f"({inserted + updated}) exceeds rows_loaded ({rows_loaded})"
        )

    expected_post_count = pre_count + inserted
    if post_count != expected_post_count:
        raise ValueError(
            f"Reconciliation failed for {domain}: expected post_count "
            f"{expected_post_count} from pre_count {pre_count} + inserted {inserted}, "
            f"got {post_count}"
        )

    result = {
        "rows_loaded": int(rows_loaded),
        "pre_count": int(pre_count),
        "post_count": int(post_count),
        "inserted": int(inserted),
        "updated": int(updated),
        "unchanged": int(rows_loaded - inserted - updated),
    }
    logger.info("Reconciliation passed for %s: %s", domain, result)
    return result


def create_and_merge_raw_table(
    bq_client: bigquery.Client, staging_table: str, raw_table: str
) -> Dict[str, int]:
    """Create raw table if needed and MERGE staging data into it."""
    create_ddl = f"""
    CREATE TABLE IF NOT EXISTS `{raw_table}` (
      game_id STRING,
      game_date DATE,
      matchup STRING,
      wl STRING,
      min FLOAT64,
      fgm FLOAT64,
      fga FLOAT64,
      fg_pct FLOAT64,
      fg3m FLOAT64,
      fg3a FLOAT64,
      fg3_pct FLOAT64,
      ftm FLOAT64,
      fta FLOAT64,
      ft_pct FLOAT64,
      oreb FLOAT64,
      dreb FLOAT64,
      pts INT64,
      reb INT64,
      ast INT64,
      stl INT64,
      blk INT64,
      tov INT64,
      pf INT64,
      plus_minus FLOAT64,
      season STRING,
      ingested_at_utc TIMESTAMP,
      player_id INT64,
      player_name STRING
    )
    PARTITION BY game_date
    CLUSTER BY player_id, player_name
    """

    stats_sql = f"""
    SELECT
      COUNTIF(t.player_id IS NULL) AS inserted,
      COUNTIF(
        t.player_id IS NOT NULL
        AND (
          COALESCE(t.game_id, '') != COALESCE(s.game_id, '')
          OR
          COALESCE(t.wl, '') != COALESCE(s.wl, '')
          OR COALESCE(t.min, 0) != COALESCE(s.min, 0)
          OR COALESCE(t.fgm, 0) != COALESCE(s.fgm, 0)
          OR COALESCE(t.fga, 0) != COALESCE(s.fga, 0)
          OR COALESCE(t.fg_pct, 0) != COALESCE(s.fg_pct, 0)
          OR COALESCE(t.fg3m, 0) != COALESCE(s.fg3m, 0)
          OR COALESCE(t.fg3a, 0) != COALESCE(s.fg3a, 0)
          OR COALESCE(t.fg3_pct, 0) != COALESCE(s.fg3_pct, 0)
          OR COALESCE(t.ftm, 0) != COALESCE(s.ftm, 0)
          OR COALESCE(t.fta, 0) != COALESCE(s.fta, 0)
          OR COALESCE(t.ft_pct, 0) != COALESCE(s.ft_pct, 0)
          OR COALESCE(t.oreb, 0) != COALESCE(s.oreb, 0)
          OR COALESCE(t.dreb, 0) != COALESCE(s.dreb, 0)
          OR COALESCE(t.pts, 0) != COALESCE(s.pts, 0)
          OR COALESCE(t.reb, 0) != COALESCE(s.reb, 0)
          OR COALESCE(t.ast, 0) != COALESCE(s.ast, 0)
          OR COALESCE(t.stl, 0) != COALESCE(s.stl, 0)
          OR COALESCE(t.blk, 0) != COALESCE(s.blk, 0)
          OR COALESCE(t.tov, 0) != COALESCE(s.tov, 0)
          OR COALESCE(t.pf, 0) != COALESCE(s.pf, 0)
          OR COALESCE(t.plus_minus, 0) != COALESCE(s.plus_minus, 0)
          OR COALESCE(t.season, '') != COALESCE(s.season, '')
          OR COALESCE(t.player_name, '') != COALESCE(s.player_name, '')
        )
      ) AS updated
    FROM `{staging_table}` s
    LEFT JOIN `{raw_table}` t
      ON t.player_id = s.player_id
     AND t.game_date = s.game_date
     AND t.matchup = s.matchup
    """

    merge_sql = f"""
    MERGE `{raw_table}` T
    USING `{staging_table}` S
    ON T.player_id = S.player_id
    AND T.game_date = S.game_date
    AND T.matchup = S.matchup
    WHEN MATCHED AND (
      COALESCE(T.game_id, '') != COALESCE(S.game_id, '')
      OR
      COALESCE(T.wl, '') != COALESCE(S.wl, '')
      OR COALESCE(T.min, 0) != COALESCE(S.min, 0)
      OR COALESCE(T.fgm, 0) != COALESCE(S.fgm, 0)
      OR COALESCE(T.fga, 0) != COALESCE(S.fga, 0)
      OR COALESCE(T.fg_pct, 0) != COALESCE(S.fg_pct, 0)
      OR COALESCE(T.fg3m, 0) != COALESCE(S.fg3m, 0)
      OR COALESCE(T.fg3a, 0) != COALESCE(S.fg3a, 0)
      OR COALESCE(T.fg3_pct, 0) != COALESCE(S.fg3_pct, 0)
      OR COALESCE(T.ftm, 0) != COALESCE(S.ftm, 0)
      OR COALESCE(T.fta, 0) != COALESCE(S.fta, 0)
      OR COALESCE(T.ft_pct, 0) != COALESCE(S.ft_pct, 0)
      OR COALESCE(T.oreb, 0) != COALESCE(S.oreb, 0)
      OR COALESCE(T.dreb, 0) != COALESCE(S.dreb, 0)
      OR COALESCE(T.pts, 0) != COALESCE(S.pts, 0)
      OR COALESCE(T.reb, 0) != COALESCE(S.reb, 0)
      OR COALESCE(T.ast, 0) != COALESCE(S.ast, 0)
      OR COALESCE(T.stl, 0) != COALESCE(S.stl, 0)
      OR COALESCE(T.blk, 0) != COALESCE(S.blk, 0)
      OR COALESCE(T.tov, 0) != COALESCE(S.tov, 0)
      OR COALESCE(T.pf, 0) != COALESCE(S.pf, 0)
      OR COALESCE(T.plus_minus, 0) != COALESCE(S.plus_minus, 0)
      OR COALESCE(T.season, '') != COALESCE(S.season, '')
      OR COALESCE(T.player_name, '') != COALESCE(S.player_name, '')
    ) THEN
      UPDATE SET
        game_id = S.game_id,
        wl = S.wl,
        min = S.min,
        fgm = S.fgm,
        fga = S.fga,
        fg_pct = S.fg_pct,
        fg3m = S.fg3m,
        fg3a = S.fg3a,
        fg3_pct = S.fg3_pct,
        ftm = S.ftm,
        fta = S.fta,
        ft_pct = S.ft_pct,
        oreb = S.oreb,
        dreb = S.dreb,
        pts = S.pts,
        reb = S.reb,
        ast = S.ast,
        stl = S.stl,
        blk = S.blk,
        tov = S.tov,
        pf = S.pf,
        plus_minus = S.plus_minus,
        season = S.season,
        ingested_at_utc = S.ingested_at_utc,
        player_name = S.player_name
    WHEN NOT MATCHED THEN
      INSERT (game_id, game_date, matchup, wl, min, fgm, fga, fg_pct, fg3m, fg3a, fg3_pct,
              ftm, fta, ft_pct, oreb, dreb, pts, reb, ast, stl, blk, tov, pf, plus_minus,
              season, ingested_at_utc, player_id, player_name)
      VALUES (S.game_id, S.game_date, S.matchup, S.wl, S.min, S.fgm, S.fga, S.fg_pct, S.fg3m, S.fg3a,
              S.fg3_pct, S.ftm, S.fta, S.ft_pct, S.oreb, S.dreb, S.pts, S.reb, S.ast, S.stl,
              S.blk, S.tov, S.pf, S.plus_minus, S.season, S.ingested_at_utc, S.player_id,
              S.player_name)
    """

    bq_client.query(create_ddl).result()
    ensure_table_has_columns(bq_client, raw_table, get_game_logs_schema())
    pre_count = (
        bq_client.query(f"SELECT COUNT(*) AS c FROM `{raw_table}`")
        .to_dataframe()
        .iloc[0]["c"]
    )
    stats = bq_client.query(stats_sql).to_dataframe().iloc[0].to_dict()
    bq_client.query(merge_sql).result()
    post_count = (
        bq_client.query(f"SELECT COUNT(*) AS c FROM `{raw_table}`")
        .to_dataframe()
        .iloc[0]["c"]
    )

    result = {
        "pre_count": int(pre_count),
        "post_count": int(post_count),
        "inserted": int(stats["inserted"]),
        "updated": int(stats["updated"]),
    }
    logger.info("MERGE completed: %s", result)
    return result


def create_and_merge_game_line_scores_table(
    bq_client: bigquery.Client, staging_table: str, raw_table: str
) -> Dict[str, int]:
    """Create line score raw table if needed and MERGE staging data into it."""
    create_ddl = f"""
    CREATE TABLE IF NOT EXISTS `{raw_table}` (
      game_date DATE,
      game_id STRING,
      season STRING,
      team_id INT64,
      team_abbr STRING,
      team_city_name STRING,
      team_nickname STRING,
      team_wins_losses STRING,
      pts_qtr1 INT64,
      pts_qtr2 INT64,
      pts_qtr3 INT64,
      pts_qtr4 INT64,
      pts_ot1 INT64,
      pts_ot2 INT64,
      pts_ot3 INT64,
      pts_ot4 INT64,
      pts_ot5 INT64,
      pts_ot6 INT64,
      pts_ot7 INT64,
      pts_ot8 INT64,
      pts_ot9 INT64,
      pts_ot10 INT64,
      pts INT64,
      ingested_at_utc TIMESTAMP
    )
    PARTITION BY game_date
    CLUSTER BY game_id, team_id
    """
    change_predicate = """
      COALESCE(T.game_date, DATE '1970-01-01') != COALESCE(S.game_date, DATE '1970-01-01')
      OR COALESCE(T.season, '') != COALESCE(S.season, '')
      OR COALESCE(T.team_abbr, '') != COALESCE(S.team_abbr, '')
      OR COALESCE(T.team_city_name, '') != COALESCE(S.team_city_name, '')
      OR COALESCE(T.team_nickname, '') != COALESCE(S.team_nickname, '')
      OR COALESCE(T.team_wins_losses, '') != COALESCE(S.team_wins_losses, '')
      OR COALESCE(T.pts_qtr1, 0) != COALESCE(S.pts_qtr1, 0)
      OR COALESCE(T.pts_qtr2, 0) != COALESCE(S.pts_qtr2, 0)
      OR COALESCE(T.pts_qtr3, 0) != COALESCE(S.pts_qtr3, 0)
      OR COALESCE(T.pts_qtr4, 0) != COALESCE(S.pts_qtr4, 0)
      OR COALESCE(T.pts_ot1, 0) != COALESCE(S.pts_ot1, 0)
      OR COALESCE(T.pts_ot2, 0) != COALESCE(S.pts_ot2, 0)
      OR COALESCE(T.pts_ot3, 0) != COALESCE(S.pts_ot3, 0)
      OR COALESCE(T.pts_ot4, 0) != COALESCE(S.pts_ot4, 0)
      OR COALESCE(T.pts_ot5, 0) != COALESCE(S.pts_ot5, 0)
      OR COALESCE(T.pts_ot6, 0) != COALESCE(S.pts_ot6, 0)
      OR COALESCE(T.pts_ot7, 0) != COALESCE(S.pts_ot7, 0)
      OR COALESCE(T.pts_ot8, 0) != COALESCE(S.pts_ot8, 0)
      OR COALESCE(T.pts_ot9, 0) != COALESCE(S.pts_ot9, 0)
      OR COALESCE(T.pts_ot10, 0) != COALESCE(S.pts_ot10, 0)
      OR COALESCE(T.pts, 0) != COALESCE(S.pts, 0)
    """
    stats_sql = f"""
    SELECT
      COUNTIF(t.game_id IS NULL) AS inserted,
      COUNTIF(t.game_id IS NOT NULL AND ({change_predicate.replace('T.', 't.').replace('S.', 's.')})) AS updated
    FROM `{staging_table}` s
    LEFT JOIN `{raw_table}` t
      ON t.game_id = s.game_id
     AND t.team_id = s.team_id
    """
    merge_sql = f"""
    MERGE `{raw_table}` T
    USING `{staging_table}` S
    ON T.game_id = S.game_id
    AND T.team_id = S.team_id
    WHEN MATCHED AND ({change_predicate}) THEN
      UPDATE SET
        game_date = S.game_date,
        season = S.season,
        team_abbr = S.team_abbr,
        team_city_name = S.team_city_name,
        team_nickname = S.team_nickname,
        team_wins_losses = S.team_wins_losses,
        pts_qtr1 = S.pts_qtr1,
        pts_qtr2 = S.pts_qtr2,
        pts_qtr3 = S.pts_qtr3,
        pts_qtr4 = S.pts_qtr4,
        pts_ot1 = S.pts_ot1,
        pts_ot2 = S.pts_ot2,
        pts_ot3 = S.pts_ot3,
        pts_ot4 = S.pts_ot4,
        pts_ot5 = S.pts_ot5,
        pts_ot6 = S.pts_ot6,
        pts_ot7 = S.pts_ot7,
        pts_ot8 = S.pts_ot8,
        pts_ot9 = S.pts_ot9,
        pts_ot10 = S.pts_ot10,
        pts = S.pts,
        ingested_at_utc = S.ingested_at_utc
    WHEN NOT MATCHED THEN
      INSERT (
        game_date, game_id, season, team_id, team_abbr, team_city_name, team_nickname,
        team_wins_losses, pts_qtr1, pts_qtr2, pts_qtr3, pts_qtr4, pts_ot1, pts_ot2,
        pts_ot3, pts_ot4, pts_ot5, pts_ot6, pts_ot7, pts_ot8, pts_ot9, pts_ot10, pts,
        ingested_at_utc
      )
      VALUES (
        S.game_date, S.game_id, S.season, S.team_id, S.team_abbr, S.team_city_name,
        S.team_nickname, S.team_wins_losses, S.pts_qtr1, S.pts_qtr2, S.pts_qtr3,
        S.pts_qtr4, S.pts_ot1, S.pts_ot2, S.pts_ot3, S.pts_ot4, S.pts_ot5, S.pts_ot6,
        S.pts_ot7, S.pts_ot8, S.pts_ot9, S.pts_ot10, S.pts, S.ingested_at_utc
      )
    """

    bq_client.query(create_ddl).result()
    ensure_table_has_columns(bq_client, raw_table, get_game_line_scores_schema())
    pre_count = (
        bq_client.query(f"SELECT COUNT(*) AS c FROM `{raw_table}`")
        .to_dataframe()
        .iloc[0]["c"]
    )
    stats = bq_client.query(stats_sql).to_dataframe().iloc[0].to_dict()
    bq_client.query(merge_sql).result()
    post_count = (
        bq_client.query(f"SELECT COUNT(*) AS c FROM `{raw_table}`")
        .to_dataframe()
        .iloc[0]["c"]
    )
    return {
        "pre_count": int(pre_count),
        "post_count": int(post_count),
        "inserted": int(stats["inserted"]),
        "updated": int(stats["updated"]),
    }


def create_and_merge_player_reference_table(
    bq_client: bigquery.Client, staging_table: str, raw_table: str
) -> Dict[str, int]:
    """Create player reference raw table if needed and MERGE staging data into it."""
    create_ddl = f"""
    CREATE TABLE IF NOT EXISTS `{raw_table}` (
      player_id INT64,
      first_name STRING,
      last_name STRING,
      player_name STRING,
      player_slug STRING,
      birthdate DATE,
      school STRING,
      country STRING,
      last_affiliation STRING,
      height STRING,
      weight INT64,
      season_exp INT64,
      jersey STRING,
      position STRING,
      roster_status BOOL,
      team_id INT64,
      team_name STRING,
      team_abbr STRING,
      team_code STRING,
      team_city STRING,
      from_year INT64,
      to_year INT64,
      draft_year STRING,
      draft_round STRING,
      draft_number STRING,
      ingested_at_utc TIMESTAMP
    )
    CLUSTER BY player_id
    """
    change_predicate = """
      COALESCE(T.first_name, '') != COALESCE(S.first_name, '')
      OR COALESCE(T.last_name, '') != COALESCE(S.last_name, '')
      OR COALESCE(T.player_name, '') != COALESCE(S.player_name, '')
      OR COALESCE(T.player_slug, '') != COALESCE(S.player_slug, '')
      OR COALESCE(T.birthdate, DATE '1970-01-01') != COALESCE(S.birthdate, DATE '1970-01-01')
      OR COALESCE(T.school, '') != COALESCE(S.school, '')
      OR COALESCE(T.country, '') != COALESCE(S.country, '')
      OR COALESCE(T.last_affiliation, '') != COALESCE(S.last_affiliation, '')
      OR COALESCE(T.height, '') != COALESCE(S.height, '')
      OR COALESCE(T.weight, -1) != COALESCE(S.weight, -1)
      OR COALESCE(T.season_exp, -1) != COALESCE(S.season_exp, -1)
      OR COALESCE(T.jersey, '') != COALESCE(S.jersey, '')
      OR COALESCE(T.position, '') != COALESCE(S.position, '')
      OR COALESCE(T.roster_status, FALSE) != COALESCE(S.roster_status, FALSE)
      OR COALESCE(T.team_id, -1) != COALESCE(S.team_id, -1)
      OR COALESCE(T.team_name, '') != COALESCE(S.team_name, '')
      OR COALESCE(T.team_abbr, '') != COALESCE(S.team_abbr, '')
      OR COALESCE(T.team_code, '') != COALESCE(S.team_code, '')
      OR COALESCE(T.team_city, '') != COALESCE(S.team_city, '')
      OR COALESCE(T.from_year, -1) != COALESCE(S.from_year, -1)
      OR COALESCE(T.to_year, -1) != COALESCE(S.to_year, -1)
      OR COALESCE(T.draft_year, '') != COALESCE(S.draft_year, '')
      OR COALESCE(T.draft_round, '') != COALESCE(S.draft_round, '')
      OR COALESCE(T.draft_number, '') != COALESCE(S.draft_number, '')
    """
    stats_sql = f"""
    SELECT
      COUNTIF(t.player_id IS NULL) AS inserted,
      COUNTIF(t.player_id IS NOT NULL AND ({change_predicate.replace('T.', 't.').replace('S.', 's.')})) AS updated
    FROM `{staging_table}` s
    LEFT JOIN `{raw_table}` t
      ON t.player_id = s.player_id
    """
    merge_sql = f"""
    MERGE `{raw_table}` T
    USING `{staging_table}` S
    ON T.player_id = S.player_id
    WHEN MATCHED AND ({change_predicate}) THEN
      UPDATE SET
        first_name = S.first_name,
        last_name = S.last_name,
        player_name = S.player_name,
        player_slug = S.player_slug,
        birthdate = S.birthdate,
        school = S.school,
        country = S.country,
        last_affiliation = S.last_affiliation,
        height = S.height,
        weight = S.weight,
        season_exp = S.season_exp,
        jersey = S.jersey,
        position = S.position,
        roster_status = S.roster_status,
        team_id = S.team_id,
        team_name = S.team_name,
        team_abbr = S.team_abbr,
        team_code = S.team_code,
        team_city = S.team_city,
        from_year = S.from_year,
        to_year = S.to_year,
        draft_year = S.draft_year,
        draft_round = S.draft_round,
        draft_number = S.draft_number,
        ingested_at_utc = S.ingested_at_utc
    WHEN NOT MATCHED THEN
      INSERT (
        player_id, first_name, last_name, player_name, player_slug, birthdate,
        school, country, last_affiliation, height, weight, season_exp, jersey,
        position, roster_status, team_id, team_name, team_abbr, team_code,
        team_city, from_year, to_year, draft_year, draft_round, draft_number,
        ingested_at_utc
      )
      VALUES (
        S.player_id, S.first_name, S.last_name, S.player_name, S.player_slug,
        S.birthdate, S.school, S.country, S.last_affiliation, S.height, S.weight,
        S.season_exp, S.jersey, S.position, S.roster_status, S.team_id, S.team_name,
        S.team_abbr, S.team_code, S.team_city, S.from_year, S.to_year, S.draft_year,
        S.draft_round, S.draft_number, S.ingested_at_utc
      )
    """

    bq_client.query(create_ddl).result()
    ensure_table_has_columns(bq_client, raw_table, get_player_reference_schema())
    pre_count = (
        bq_client.query(f"SELECT COUNT(*) AS c FROM `{raw_table}`")
        .to_dataframe()
        .iloc[0]["c"]
    )
    stats = bq_client.query(stats_sql).to_dataframe().iloc[0].to_dict()
    bq_client.query(merge_sql).result()
    post_count = (
        bq_client.query(f"SELECT COUNT(*) AS c FROM `{raw_table}`")
        .to_dataframe()
        .iloc[0]["c"]
    )
    return {
        "pre_count": int(pre_count),
        "post_count": int(post_count),
        "inserted": int(stats["inserted"]),
        "updated": int(stats["updated"]),
    }


def get_schedule_schema() -> List[bigquery.SchemaField]:
    """Return the BigQuery schema for upcoming team schedule rows."""
    return [
        bigquery.SchemaField("SCHEDULE_DATE", "DATE"),
        bigquery.SchemaField("GAME_ID", "STRING"),
        bigquery.SchemaField("SEASON", "STRING"),
        bigquery.SchemaField("TEAM_ABBR", "STRING"),
        bigquery.SchemaField("OPPONENT_ABBR", "STRING"),
        bigquery.SchemaField("HOME_AWAY", "STRING"),
        bigquery.SchemaField("IS_BACK_TO_BACK", "BOOLEAN"),
        bigquery.SchemaField("GAME_STATUS", "STRING"),
        bigquery.SchemaField("SOURCE_UPDATED_AT_UTC", "TIMESTAMP"),
        bigquery.SchemaField("INGESTED_AT_UTC", "TIMESTAMP"),
    ]


def get_upcoming_schedule(
    *,
    season: str = SUPPORTED_SEASON,
    horizon_days: int = 7,
    today: Any = None,
    retries: int = NBA_API_RETRIES,
    timeout: float = NBA_API_TIMEOUT_SECONDS,
    retry_base_delay: float = NBA_API_RETRY_BASE_DELAY_SECONDS,
    retry_backoff_multiplier: float = NBA_API_RETRY_BACKOFF_MULTIPLIER,
    retry_max_delay: float = NBA_API_RETRY_MAX_DELAY_SECONDS,
) -> pd.DataFrame:
    """Fetch upcoming schedule rows from nba_api scheduleleaguev2."""
    if season != SUPPORTED_SEASON:
        raise ValueError(f"Unsupported production season: {season}")

    base_day = coerce_to_date(today) or pd.Timestamp.now(tz="UTC").date()
    end_day = base_day + timedelta(days=max(horizon_days, 1) - 1)
    retries = normalize_nba_api_retries(retries)
    empty = pd.DataFrame(columns=[field.name for field in get_schedule_schema()])
    frames = []
    for attempt in range(1, retries + 1):
        try:
            schedule = scheduleleaguev2.ScheduleLeagueV2(
                season=season,
                timeout=timeout,
            )
            frames = schedule.get_data_frames()
            break
        except Exception:
            if attempt == retries:
                logger.exception(
                    "Failed NBA API schedule season=%s after %s attempts timeout=%.1fs",
                    season,
                    retries,
                    timeout,
                )
                return empty.copy()
            _sleep_before_nba_api_retry(
                domain="schedule",
                identifier=season,
                attempt=attempt,
                retries=retries,
                timeout=timeout,
                retry_base_delay=retry_base_delay,
                retry_backoff_multiplier=retry_backoff_multiplier,
                retry_max_delay=retry_max_delay,
            )
    if not frames:
        return empty.copy()

    raw = frames[0].copy()
    if raw.empty:
        return empty.copy()

    raw["gameDate"] = pd.to_datetime(raw["gameDate"], errors="coerce").dt.date
    raw["gameDateTimeUTC"] = pd.to_datetime(
        raw["gameDateTimeUTC"], errors="coerce", utc=True
    )
    raw = raw[(raw["gameDate"] >= base_day) & (raw["gameDate"] <= end_day)].copy()

    if raw.empty:
        return empty.copy()

    ingested_at = pd.Timestamp.now(tz="UTC")
    team_rows: list[dict[str, Any]] = []
    for row in raw.to_dict("records"):
        game_date = row.get("gameDate")
        game_id = str(row.get("gameId", "") or "")
        source_updated_at = row.get("gameDateTimeUTC")
        game_status = str(row.get("gameStatusText", "") or "")
        season_value = season
        home_team = str(row.get("homeTeam_teamTricode", "") or "").upper()
        away_team = str(row.get("awayTeam_teamTricode", "") or "").upper()
        if not game_id or not game_date or not home_team or not away_team:
            continue
        team_rows.extend(
            [
                {
                    "SCHEDULE_DATE": game_date,
                    "GAME_ID": game_id,
                    "SEASON": season_value,
                    "TEAM_ABBR": home_team,
                    "OPPONENT_ABBR": away_team,
                    "HOME_AWAY": "HOME",
                    "IS_BACK_TO_BACK": False,
                    "GAME_STATUS": game_status,
                    "SOURCE_UPDATED_AT_UTC": source_updated_at,
                    "INGESTED_AT_UTC": ingested_at,
                },
                {
                    "SCHEDULE_DATE": game_date,
                    "GAME_ID": game_id,
                    "SEASON": season_value,
                    "TEAM_ABBR": away_team,
                    "OPPONENT_ABBR": home_team,
                    "HOME_AWAY": "AWAY",
                    "IS_BACK_TO_BACK": False,
                    "GAME_STATUS": game_status,
                    "SOURCE_UPDATED_AT_UTC": source_updated_at,
                    "INGESTED_AT_UTC": ingested_at,
                },
            ]
        )

    df = pd.DataFrame(team_rows)
    if df.empty:
        return empty.copy()
    df = df.drop_duplicates(subset=["GAME_ID", "TEAM_ABBR"]).copy()
    df["SCHEDULE_DATE"] = pd.to_datetime(df["SCHEDULE_DATE"], errors="coerce")
    df = df.sort_values(["TEAM_ABBR", "SCHEDULE_DATE", "GAME_ID"]).reset_index(
        drop=True
    )
    prev_dates = df.groupby("TEAM_ABBR")["SCHEDULE_DATE"].shift(1)
    df["IS_BACK_TO_BACK"] = prev_dates.notna() & (
        (df["SCHEDULE_DATE"] - prev_dates).dt.days == 1
    )
    df["SCHEDULE_DATE"] = df["SCHEDULE_DATE"].dt.date
    return df.reset_index(drop=True)


def run_schedule_quality_checks(
    bq_client: bigquery.Client,
    staging_table: str,
    *,
    season: str = SUPPORTED_SEASON,
) -> dict:
    """Run data quality checks on schedule staging table."""
    dq_query = f"""
    WITH base AS (
      SELECT *
      FROM `{staging_table}`
    ),
    dups AS (
      SELECT COUNT(*) AS duplicate_keys
      FROM (
        SELECT game_id, team_abbr, COUNT(*) AS cnt
        FROM base
        GROUP BY game_id, team_abbr
        HAVING COUNT(*) > 1
      )
    )
    SELECT
      (SELECT COUNT(*) FROM base) AS total_rows,
      (SELECT COUNT(*) FROM base WHERE schedule_date IS NULL OR game_id IS NULL OR team_abbr IS NULL) AS null_key_rows,
      (SELECT duplicate_keys FROM dups) AS duplicate_key_rows,
      (SELECT COUNT(*) FROM base WHERE season != @season OR season IS NULL) AS invalid_season_rows,
      (SELECT COUNT(*) FROM base WHERE home_away IS NULL OR upper(home_away) NOT IN ('HOME', 'AWAY')) AS invalid_home_away_rows
    """
    dq = (
        bq_client.query(
            dq_query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("season", "STRING", season),
                ]
            ),
        )
        .to_dataframe()
        .iloc[0]
        .to_dict()
    )
    if dq["null_key_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['null_key_rows']} schedule rows with null keys"
        )
    if dq["duplicate_key_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['duplicate_key_rows']} duplicate schedule rows"
        )
    if dq["invalid_season_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['invalid_season_rows']} schedule rows outside season {season}"
        )
    if dq["invalid_home_away_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['invalid_home_away_rows']} schedule rows with invalid home/away values"
        )
    return dq


def create_and_merge_schedule_table(
    bq_client: bigquery.Client, staging_table: str, raw_table: str
) -> Dict[str, int]:
    """Create schedule raw table if needed and merge staging data into it."""
    create_ddl = f"""
    CREATE TABLE IF NOT EXISTS `{raw_table}` (
      schedule_date DATE,
      game_id STRING,
      season STRING,
      team_abbr STRING,
      opponent_abbr STRING,
      home_away STRING,
      is_back_to_back BOOL,
      game_status STRING,
      source_updated_at_utc TIMESTAMP,
      ingested_at_utc TIMESTAMP
    )
    PARTITION BY schedule_date
    CLUSTER BY team_abbr, game_id
    """
    stats_sql = f"""
    SELECT
      COUNTIF(t.game_id IS NULL) AS inserted,
      COUNTIF(
        t.game_id IS NOT NULL
        AND (
          COALESCE(t.opponent_abbr, '') != COALESCE(s.opponent_abbr, '')
          OR COALESCE(t.home_away, '') != COALESCE(s.home_away, '')
          OR COALESCE(t.is_back_to_back, FALSE) != COALESCE(s.is_back_to_back, FALSE)
          OR COALESCE(t.game_status, '') != COALESCE(s.game_status, '')
          OR COALESCE(t.source_updated_at_utc, TIMESTAMP('1970-01-01')) != COALESCE(s.source_updated_at_utc, TIMESTAMP('1970-01-01'))
        )
      ) AS updated
    FROM `{staging_table}` s
    LEFT JOIN `{raw_table}` t
      ON t.game_id = s.game_id
     AND t.team_abbr = s.team_abbr
    """
    merge_sql = f"""
    MERGE `{raw_table}` T
    USING `{staging_table}` S
    ON T.game_id = S.game_id
    AND T.team_abbr = S.team_abbr
    WHEN MATCHED AND (
      COALESCE(T.opponent_abbr, '') != COALESCE(S.opponent_abbr, '')
      OR COALESCE(T.home_away, '') != COALESCE(S.home_away, '')
      OR COALESCE(T.is_back_to_back, FALSE) != COALESCE(S.is_back_to_back, FALSE)
      OR COALESCE(T.game_status, '') != COALESCE(S.game_status, '')
      OR COALESCE(T.source_updated_at_utc, TIMESTAMP('1970-01-01')) != COALESCE(S.source_updated_at_utc, TIMESTAMP('1970-01-01'))
    ) THEN UPDATE SET
      schedule_date = S.schedule_date,
      season = S.season,
      opponent_abbr = S.opponent_abbr,
      home_away = S.home_away,
      is_back_to_back = S.is_back_to_back,
      game_status = S.game_status,
      source_updated_at_utc = S.source_updated_at_utc,
      ingested_at_utc = S.ingested_at_utc
    WHEN NOT MATCHED THEN
      INSERT (schedule_date, game_id, season, team_abbr, opponent_abbr, home_away, is_back_to_back, game_status, source_updated_at_utc, ingested_at_utc)
      VALUES (S.schedule_date, S.game_id, S.season, S.team_abbr, S.opponent_abbr, S.home_away, S.is_back_to_back, S.game_status, S.source_updated_at_utc, S.ingested_at_utc)
    """
    bq_client.query(create_ddl).result()
    ensure_table_has_columns(bq_client, raw_table, get_schedule_schema())
    pre_count = (
        bq_client.query(f"SELECT COUNT(*) AS c FROM `{raw_table}`")
        .to_dataframe()
        .iloc[0]["c"]
    )
    stats = bq_client.query(stats_sql).to_dataframe().iloc[0].to_dict()
    bq_client.query(merge_sql).result()
    post_count = (
        bq_client.query(f"SELECT COUNT(*) AS c FROM `{raw_table}`")
        .to_dataframe()
        .iloc[0]["c"]
    )
    return {
        "pre_count": int(pre_count),
        "post_count": int(post_count),
        "inserted": int(stats["inserted"]),
        "updated": int(stats["updated"]),
    }


def run_injury_report_quality_checks(
    bq_client: bigquery.Client,
    staging_table: str,
    *,
    season: str = SUPPORTED_SEASON,
) -> dict:
    """Run data quality checks on official injury report staging rows."""
    season_start, season_end = get_season_date_bounds(season)
    staging_relation = quote_bigquery_table_id(staging_table)
    dq_query = f"""
    WITH base AS (
      SELECT *
      FROM {staging_relation}
    ),
    dups AS (
      SELECT COUNT(*) AS duplicate_keys
      FROM (
        SELECT
          report_timestamp_utc,
          game_date,
          matchup,
          team_abbr,
          player_name_source,
          COUNT(*) AS cnt
        FROM base
        GROUP BY 1, 2, 3, 4, 5
        HAVING COUNT(*) > 1
      )
    )
    SELECT
      (SELECT COUNT(*) FROM base) AS total_rows,
      (
        SELECT COUNT(*)
        FROM base
        WHERE report_date IS NULL
           OR report_timestamp_utc IS NULL
           OR game_date IS NULL
           OR matchup IS NULL
           OR team_abbr IS NULL
           OR player_name_source IS NULL
           OR injury_status IS NULL
      ) AS null_key_rows,
      (SELECT duplicate_keys FROM dups) AS duplicate_key_rows,
      (SELECT COUNT(*) FROM base WHERE season != @season OR season IS NULL) AS invalid_season_rows,
      (SELECT COUNT(*) FROM base WHERE game_date < @season_start OR game_date > @season_end) AS out_of_window_rows,
      (
        SELECT COUNT(*)
        FROM base
        WHERE injury_status NOT IN ('Available', 'Probable', 'Questionable', 'Doubtful', 'Out')
      ) AS invalid_status_rows,
      (SELECT COUNT(*) FROM base WHERE player_id IS NULL) AS unmatched_player_rows
    """
    dq = (
        bq_client.query(
            dq_query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("season", "STRING", season),
                    bigquery.ScalarQueryParameter(
                        "season_start", "DATE", season_start.isoformat()
                    ),
                    bigquery.ScalarQueryParameter(
                        "season_end", "DATE", season_end.isoformat()
                    ),
                ]
            ),
        )
        .to_dataframe()
        .iloc[0]
        .to_dict()
    )
    if dq["total_rows"] == 0:
        raise ValueError("DQ failed: injury report staging table has zero rows")
    if dq["null_key_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['null_key_rows']} injury report rows with null keys"
        )
    if dq["duplicate_key_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['duplicate_key_rows']} duplicate injury report rows"
        )
    if dq["invalid_season_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['invalid_season_rows']} injury rows outside season {season}"
        )
    if dq["out_of_window_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['out_of_window_rows']} injury rows outside date window "
            f"{season_start.isoformat()} to {season_end.isoformat()}"
        )
    if dq["invalid_status_rows"] > 0:
        raise ValueError(
            f"DQ failed: found {dq['invalid_status_rows']} invalid injury statuses"
        )
    return dq


def create_and_merge_injury_report_table(
    bq_client: bigquery.Client, staging_table: str, raw_table: str
) -> Dict[str, int]:
    """Create official injury report raw table if needed and merge staging rows."""
    staging_relation = quote_bigquery_table_id(staging_table)
    raw_relation = quote_bigquery_table_id(raw_table)
    create_ddl = f"""
    CREATE TABLE IF NOT EXISTS {raw_relation} (
      report_date DATE,
      report_time_et STRING,
      report_timestamp_utc TIMESTAMP,
      game_date DATE,
      game_time_et STRING,
      matchup STRING,
      season STRING,
      team_abbr STRING,
      team_name STRING,
      player_id INT64,
      player_name STRING,
      player_name_source STRING,
      injury_status STRING,
      reason STRING,
      source_url STRING,
      source_system STRING,
      ingested_at_utc TIMESTAMP
    )
    PARTITION BY report_date
    CLUSTER BY team_abbr, player_id
    """
    change_predicate = """
      COALESCE(T.report_time_et, '') != COALESCE(S.report_time_et, '')
      OR COALESCE(T.game_time_et, '') != COALESCE(S.game_time_et, '')
      OR COALESCE(T.season, '') != COALESCE(S.season, '')
      OR COALESCE(T.team_name, '') != COALESCE(S.team_name, '')
      OR COALESCE(T.player_id, -1) != COALESCE(S.player_id, -1)
      OR COALESCE(T.player_name, '') != COALESCE(S.player_name, '')
      OR COALESCE(T.injury_status, '') != COALESCE(S.injury_status, '')
      OR COALESCE(T.reason, '') != COALESCE(S.reason, '')
      OR COALESCE(T.source_url, '') != COALESCE(S.source_url, '')
      OR COALESCE(T.source_system, '') != COALESCE(S.source_system, '')
    """
    stats_sql = f"""
    SELECT
      COUNTIF(t.report_timestamp_utc IS NULL) AS inserted,
      COUNTIF(t.report_timestamp_utc IS NOT NULL AND ({change_predicate.replace('T.', 't.').replace('S.', 's.')})) AS updated
    FROM {staging_relation} s
    LEFT JOIN {raw_relation} t
      ON t.report_timestamp_utc = s.report_timestamp_utc
     AND t.game_date = s.game_date
     AND t.matchup = s.matchup
     AND t.team_abbr = s.team_abbr
     AND t.player_name_source = s.player_name_source
    """
    merge_sql = f"""
    MERGE {raw_relation} T
    USING {staging_relation} S
    ON T.report_timestamp_utc = S.report_timestamp_utc
    AND T.game_date = S.game_date
    AND T.matchup = S.matchup
    AND T.team_abbr = S.team_abbr
    AND T.player_name_source = S.player_name_source
    WHEN MATCHED AND ({change_predicate}) THEN UPDATE SET
      report_date = S.report_date,
      report_time_et = S.report_time_et,
      game_time_et = S.game_time_et,
      season = S.season,
      team_name = S.team_name,
      player_id = S.player_id,
      player_name = S.player_name,
      injury_status = S.injury_status,
      reason = S.reason,
      source_url = S.source_url,
      source_system = S.source_system,
      ingested_at_utc = S.ingested_at_utc
    WHEN NOT MATCHED THEN
      INSERT (
        report_date, report_time_et, report_timestamp_utc, game_date, game_time_et,
        matchup, season, team_abbr, team_name, player_id, player_name,
        player_name_source, injury_status, reason, source_url, source_system,
        ingested_at_utc
      )
      VALUES (
        S.report_date, S.report_time_et, S.report_timestamp_utc, S.game_date,
        S.game_time_et, S.matchup, S.season, S.team_abbr, S.team_name, S.player_id,
        S.player_name, S.player_name_source, S.injury_status, S.reason, S.source_url,
        S.source_system, S.ingested_at_utc
      )
    """
    bq_client.query(create_ddl).result()
    ensure_table_has_columns(bq_client, raw_table, get_injury_report_schema())
    pre_count = (
        bq_client.query(f"SELECT COUNT(*) AS c FROM {raw_relation}")
        .to_dataframe()
        .iloc[0]["c"]
    )
    stats = bq_client.query(stats_sql).to_dataframe().iloc[0].to_dict()
    bq_client.query(merge_sql).result()
    post_count = (
        bq_client.query(f"SELECT COUNT(*) AS c FROM {raw_relation}")
        .to_dataframe()
        .iloc[0]["c"]
    )
    return {
        "pre_count": int(pre_count),
        "post_count": int(post_count),
        "inserted": int(stats["inserted"]),
        "updated": int(stats["updated"]),
    }


def create_analysis_snapshot_table(
    bq_client: bigquery.Client,
    table_id: str,
) -> None:
    """Create the deterministic analysis snapshot table if it does not exist."""
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{table_id}` (
      snapshot_id STRING,
      snapshot_date DATE,
      created_at_utc TIMESTAMP,
      season STRING,
      headline STRING,
      dek STRING,
      body STRING,
      trend_player STRING,
      trend_stat STRING,
      trend_delta FLOAT64,
      contribution_player_id INT64,
      contribution_player_name STRING,
      contribution_team_abbr STRING,
      contribution_opponent_abbr STRING,
      contribution_matchup STRING,
      contribution_player_pts INT64,
      contribution_team_pts INT64,
      contribution_opponent_team_pts INT64,
      contribution_player_points_share_of_team FLOAT64,
      contribution_player_points_share_of_game FLOAT64,
      contribution_scoring_margin INT64,
      contribution_team_pts_qtr1 INT64,
      contribution_team_pts_qtr2 INT64,
      contribution_team_pts_qtr3 INT64,
      contribution_team_pts_qtr4 INT64,
      contribution_team_pts_ot_total INT64,
      contribution_game_date DATE,
      context_player_id INT64,
      context_player_name STRING,
      context_team_abbr STRING,
      context_team_name STRING,
      context_position STRING,
      context_height STRING,
      context_weight INT64,
      context_roster_status BOOL,
      context_season_exp INT64,
      context_draft_year STRING,
      context_draft_round STRING,
      context_draft_number STRING,
      freshness_ts TIMESTAMP,
      source_run_id STRING
    )
    PARTITION BY snapshot_date
    CLUSTER BY season
    """
    bq_client.query(ddl).result()
    for column_ddl in [
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS contribution_player_id INT64",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS contribution_player_name STRING",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS contribution_team_abbr STRING",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS contribution_opponent_abbr STRING",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS contribution_matchup STRING",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS contribution_player_pts INT64",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS contribution_team_pts INT64",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS contribution_opponent_team_pts INT64",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS contribution_player_points_share_of_team FLOAT64",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS contribution_player_points_share_of_game FLOAT64",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS contribution_scoring_margin INT64",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS contribution_team_pts_qtr1 INT64",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS contribution_team_pts_qtr2 INT64",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS contribution_team_pts_qtr3 INT64",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS contribution_team_pts_qtr4 INT64",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS contribution_team_pts_ot_total INT64",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS contribution_game_date DATE",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS context_player_id INT64",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS context_player_name STRING",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS context_team_abbr STRING",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS context_team_name STRING",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS context_position STRING",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS context_height STRING",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS context_weight INT64",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS context_roster_status BOOL",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS context_season_exp INT64",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS context_draft_year STRING",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS context_draft_round STRING",
        "ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS context_draft_number STRING",
    ]:
        bq_client.query(column_ddl.format(table=table_id)).result()


def build_analysis_snapshot_record(
    *,
    season: str,
    daily_leaders: pd.DataFrame,
    trends: pd.DataFrame,
    recommendations: Optional[pd.DataFrame] = None,
    rankings: Optional[pd.DataFrame] = None,
    score_contribution: Optional[pd.DataFrame] = None,
    player_context: Optional[pd.DataFrame] = None,
    source_run_id: str,
    created_at_utc: Any = None,
    snapshot_date: Any = None,
    freshness_ts: Any = None,
) -> Dict[str, Any]:
    """Build a deterministic analysis snapshot from gold outputs."""
    if season != SUPPORTED_SEASON:
        raise ValueError(f"Analysis snapshots only support season {SUPPORTED_SEASON}")
    created_at = pd.to_datetime(created_at_utc or pd.Timestamp.now(tz="UTC"), utc=True)

    leaders = daily_leaders.copy()
    if leaders.empty:
        raise ValueError("Cannot build analysis snapshot without leaderboard data")
    leaders["game_date"] = pd.to_datetime(leaders["game_date"], errors="coerce")
    leaders = leaders.dropna(subset=["game_date"]).sort_values(
        ["game_date", "pts"], ascending=[False, False]
    )
    if leaders.empty:
        raise ValueError("Cannot build analysis snapshot from invalid leaderboard rows")

    latest_row = leaders.iloc[0]
    snapshot_day = coerce_to_date(snapshot_date) or created_at.date()
    latest_game_date = latest_row["game_date"].date()
    recommendations = (
        recommendations.copy() if recommendations is not None else pd.DataFrame()
    )
    rankings = rankings.copy() if rankings is not None else pd.DataFrame()
    score_contribution = (
        score_contribution.copy() if score_contribution is not None else pd.DataFrame()
    )
    player_context = (
        player_context.copy() if player_context is not None else pd.DataFrame()
    )

    trend_player = ""
    trend_stat = ""
    trend_delta = 0.0
    trend_sentence = "No player trend qualified for the latest snapshot window."
    contribution_sentence = (
        "No direct scoring contribution story qualified for the latest snapshot window."
    )
    context_sentence = "No enriched player context is available for the featured story."
    recommendation_sentence = (
        "No fantasy recommendation qualified for the latest snapshot window."
    )
    ranking_sentence = "No fantasy ranking summary is available yet."
    contribution_payload = {
        "player_id": None,
        "player_name": None,
        "team_abbr": None,
        "opponent_abbr": None,
        "matchup": None,
        "player_pts": None,
        "team_pts": None,
        "opponent_team_pts": None,
        "player_points_share_of_team": None,
        "player_points_share_of_game": None,
        "scoring_margin": None,
        "team_pts_qtr1": None,
        "team_pts_qtr2": None,
        "team_pts_qtr3": None,
        "team_pts_qtr4": None,
        "team_pts_ot_total": None,
        "game_date": None,
    }
    context_payload = {
        "player_id": None,
        "player_name": None,
        "team_abbr": None,
        "team_name": None,
        "position": None,
        "height": None,
        "weight": None,
        "roster_status": None,
        "season_exp": None,
        "draft_year": None,
        "draft_round": None,
        "draft_number": None,
    }

    if not trends.empty:
        trend_working = trends.copy()
        trend_working["delta"] = pd.to_numeric(trend_working["delta"], errors="coerce")
        trend_working = trend_working.dropna(subset=["delta"]).copy()
        if not trend_working.empty:
            trend_working["abs_delta"] = trend_working["delta"].abs()
            trend_working = trend_working.sort_values(
                ["abs_delta", "player_name", "stat"],
                ascending=[False, True, True],
            )
            top_trend = trend_working.iloc[0]
            trend_player = str(top_trend["player_name"])
            trend_stat = str(top_trend["stat"])
            trend_delta = round(float(top_trend["delta"]), 1)
            recent_avg = round(float(top_trend["recent_avg"]), 1)
            prior_avg = round(float(top_trend["prior_avg"]), 1)
            direction = "up" if trend_delta >= 0 else "down"
            trend_sentence = (
                f"{trend_player} is trending {direction} in {trend_stat}, moving from "
                f"{prior_avg:.1f} to {recent_avg:.1f} per game ({trend_delta:+.1f})."
            )

    top_recommendation = None
    if not recommendations.empty:
        recommendation_working = recommendations.copy()
        recommendation_working["priority_score"] = pd.to_numeric(
            recommendation_working["priority_score"], errors="coerce"
        )
        recommendation_working["confidence_score"] = pd.to_numeric(
            recommendation_working["confidence_score"], errors="coerce"
        )
        recommendation_working = recommendation_working.dropna(
            subset=["priority_score", "confidence_score"]
        ).copy()
        if not recommendation_working.empty:
            recommendation_working = recommendation_working.sort_values(
                ["priority_score", "confidence_score", "player_name"],
                ascending=[False, False, True],
            )
            top_recommendation = recommendation_working.iloc[0]
            recommendation_sentence = (
                f"Top fantasy signal: {top_recommendation['player_name']} profiles as "
                f"{top_recommendation['insight_type']} with recommendation "
                f"{top_recommendation['recommendation']} and priority "
                f"{float(top_recommendation['priority_score']):.1f}."
            )

    if not rankings.empty:
        ranking_working = rankings.copy()
        rank_col = (
            "fantasy_rank_9cat_proxy"
            if "fantasy_rank_9cat_proxy" in ranking_working.columns
            else "overall_rank"
        )
        if rank_col in ranking_working.columns:
            ranking_working[rank_col] = pd.to_numeric(
                ranking_working[rank_col], errors="coerce"
            )
            ranking_working = ranking_working.dropna(subset=[rank_col]).copy()
            if not ranking_working.empty:
                ranking_working = ranking_working.sort_values(
                    [rank_col, "player_name"], ascending=[True, True]
                )
                top_ranked = ranking_working.iloc[0]
                ranking_sentence = (
                    f"Current fantasy leader: {top_ranked['player_name']} sits at rank "
                    f"{int(top_ranked[rank_col])} with tier "
                    f"{top_ranked.get('recommendation_tier', 'n/a')}."
                )

    featured_contribution = None
    if not score_contribution.empty:
        contribution_working = score_contribution.copy()
        contribution_working["game_date"] = pd.to_datetime(
            contribution_working["game_date"], errors="coerce"
        )
        for col in ("player_points_share_of_team", "player_points_share_of_game"):
            if col in contribution_working.columns:
                contribution_working[col] = pd.to_numeric(
                    contribution_working[col], errors="coerce"
                )
        if "player_pts" in contribution_working.columns:
            contribution_working["player_pts"] = pd.to_numeric(
                contribution_working["player_pts"], errors="coerce"
            )
        contribution_working = contribution_working.dropna(
            subset=["game_date", "player_points_share_of_team", "player_pts"]
        ).copy()
        if not contribution_working.empty:
            contribution_working = contribution_working.sort_values(
                [
                    "game_date",
                    "player_points_share_of_team",
                    "player_pts",
                    "player_name",
                ],
                ascending=[False, False, False, True],
            )
            featured_contribution = contribution_working.iloc[0]
            contribution_payload = {
                "player_id": int(featured_contribution["player_id"])
                if pd.notna(featured_contribution.get("player_id"))
                else None,
                "player_name": str(featured_contribution.get("player_name") or ""),
                "team_abbr": str(featured_contribution.get("team_abbr") or ""),
                "opponent_abbr": str(featured_contribution.get("opponent_abbr") or ""),
                "matchup": str(featured_contribution.get("matchup") or ""),
                "player_pts": int(featured_contribution["player_pts"])
                if pd.notna(featured_contribution.get("player_pts"))
                else None,
                "team_pts": int(featured_contribution["team_pts"])
                if pd.notna(featured_contribution.get("team_pts"))
                else None,
                "opponent_team_pts": int(featured_contribution["opponent_team_pts"])
                if pd.notna(featured_contribution.get("opponent_team_pts"))
                else None,
                "player_points_share_of_team": round(
                    float(featured_contribution["player_points_share_of_team"]), 4
                ),
                "player_points_share_of_game": round(
                    float(featured_contribution["player_points_share_of_game"]), 4
                )
                if pd.notna(featured_contribution.get("player_points_share_of_game"))
                else None,
                "scoring_margin": int(featured_contribution["scoring_margin"])
                if pd.notna(featured_contribution.get("scoring_margin"))
                else None,
                "team_pts_qtr1": int(featured_contribution["team_pts_qtr1"])
                if pd.notna(featured_contribution.get("team_pts_qtr1"))
                else None,
                "team_pts_qtr2": int(featured_contribution["team_pts_qtr2"])
                if pd.notna(featured_contribution.get("team_pts_qtr2"))
                else None,
                "team_pts_qtr3": int(featured_contribution["team_pts_qtr3"])
                if pd.notna(featured_contribution.get("team_pts_qtr3"))
                else None,
                "team_pts_qtr4": int(featured_contribution["team_pts_qtr4"])
                if pd.notna(featured_contribution.get("team_pts_qtr4"))
                else None,
                "team_pts_ot_total": int(featured_contribution["team_pts_ot_total"])
                if pd.notna(featured_contribution.get("team_pts_ot_total"))
                else None,
                "game_date": featured_contribution["game_date"].date().isoformat(),
            }
            contribution_sentence = (
                f"{contribution_payload['player_name']} supplied "
                f"{contribution_payload['player_pts']} of {contribution_payload['team_pts']} "
                f"{contribution_payload['team_abbr']} points against "
                f"{contribution_payload['opponent_abbr']}, a "
                f"{contribution_payload['player_points_share_of_team']:.1%} share of team scoring. "
                f"Quarter totals landed at {contribution_payload['team_pts_qtr1']}-"
                f"{contribution_payload['team_pts_qtr2']}-"
                f"{contribution_payload['team_pts_qtr3']}-"
                f"{contribution_payload['team_pts_qtr4']}"
                + (
                    f" with {contribution_payload['team_pts_ot_total']} overtime points."
                    if contribution_payload["team_pts_ot_total"]
                    else "."
                )
            )

    if featured_contribution is not None and not player_context.empty:
        context_working = player_context.copy()
        if "player_id" in context_working.columns:
            context_working["player_id"] = pd.to_numeric(
                context_working["player_id"], errors="coerce"
            )
            context_working = context_working[
                context_working["player_id"] == contribution_payload["player_id"]
            ].copy()
        if not context_working.empty:
            featured_context = context_working.iloc[0]
            roster_status = featured_context.get("roster_status")
            if isinstance(roster_status, str):
                roster_status = roster_status.lower() == "true"
            context_payload = {
                "player_id": int(featured_context["player_id"])
                if pd.notna(featured_context.get("player_id"))
                else None,
                "player_name": str(featured_context.get("player_name") or ""),
                "team_abbr": str(featured_context.get("latest_team_abbr") or ""),
                "team_name": str(featured_context.get("team_name") or ""),
                "position": str(featured_context.get("position") or ""),
                "height": str(featured_context.get("height") or ""),
                "weight": int(featured_context["weight"])
                if pd.notna(featured_context.get("weight"))
                else None,
                "roster_status": bool(roster_status)
                if roster_status in (True, False)
                else None,
                "season_exp": int(featured_context["season_exp"])
                if pd.notna(featured_context.get("season_exp"))
                else None,
                "draft_year": str(featured_context.get("draft_year") or ""),
                "draft_round": str(featured_context.get("draft_round") or ""),
                "draft_number": str(featured_context.get("draft_number") or ""),
            }
            roster_label = (
                "active"
                if context_payload["roster_status"]
                else "inactive"
                if context_payload["roster_status"] is not None
                else "unknown roster status"
            )
            context_sentence = (
                f"{context_payload['player_name']} is listed as a "
                f"{context_payload['position']} for {context_payload['team_name']} "
                f"({context_payload['team_abbr']}), stands {context_payload['height']}, "
                f"weighs {context_payload['weight']} pounds, and carries {roster_label} "
                f"status with {context_payload['season_exp']} seasons of experience."
            )

    headline = (
        f"{contribution_payload['player_name']} drives the latest {season} scoring snapshot"
        if featured_contribution is not None
        else f"{top_recommendation['player_name']} headlines the {season} fantasy board"
        if top_recommendation is not None
        else (
            f"{latest_row['pts_leader']} sets the pace for the {season} nightly board"
            if not trend_player
            else f"{trend_player} headlines the {season} trend watch"
        )
    )
    dek = (
        f"Latest leaders from {latest_game_date.isoformat()} are anchored by "
        f"{latest_row['pts_leader']} in scoring, {latest_row['reb_leader']} on the glass, "
        f"and {latest_row['ast_leader']} as the top playmaker. "
        f"{contribution_sentence if featured_contribution is not None else recommendation_sentence}"
    )
    body = "\n\n".join(
        [
            (
                f"The latest completed game day in the {season} warehouse is {latest_game_date.isoformat()}. "
                f"{latest_row['pts_leader']} led scoring with {int(latest_row['pts'])} points in "
                f"{latest_row['pts_matchup']}, while {latest_row['reb_leader']} posted "
                f"{int(latest_row['reb'])} rebounds and {latest_row['ast_leader']} handed out "
                f"{int(latest_row['ast'])} assists."
            ),
            contribution_sentence,
            context_sentence,
            trend_sentence,
            ranking_sentence,
            recommendation_sentence,
            (
                f"This snapshot was generated deterministically from gold tables and linked to "
                f"pipeline run {source_run_id}. Freshness is measured from "
                f"{pd.to_datetime(freshness_ts or created_at, utc=True).isoformat()}."
            ),
        ]
    )

    return {
        "snapshot_id": f"{season.replace('-', '')}_{snapshot_day.strftime('%Y%m%d')}",
        "snapshot_date": snapshot_day.isoformat(),
        "created_at_utc": created_at.isoformat(),
        "season": season,
        "headline": headline,
        "dek": dek,
        "body": body,
        "trend_player": trend_player,
        "trend_stat": trend_stat,
        "trend_delta": trend_delta,
        "contribution_player_id": contribution_payload["player_id"],
        "contribution_player_name": contribution_payload["player_name"],
        "contribution_team_abbr": contribution_payload["team_abbr"],
        "contribution_opponent_abbr": contribution_payload["opponent_abbr"],
        "contribution_matchup": contribution_payload["matchup"],
        "contribution_player_pts": contribution_payload["player_pts"],
        "contribution_team_pts": contribution_payload["team_pts"],
        "contribution_opponent_team_pts": contribution_payload["opponent_team_pts"],
        "contribution_player_points_share_of_team": contribution_payload[
            "player_points_share_of_team"
        ],
        "contribution_player_points_share_of_game": contribution_payload[
            "player_points_share_of_game"
        ],
        "contribution_scoring_margin": contribution_payload["scoring_margin"],
        "contribution_team_pts_qtr1": contribution_payload["team_pts_qtr1"],
        "contribution_team_pts_qtr2": contribution_payload["team_pts_qtr2"],
        "contribution_team_pts_qtr3": contribution_payload["team_pts_qtr3"],
        "contribution_team_pts_qtr4": contribution_payload["team_pts_qtr4"],
        "contribution_team_pts_ot_total": contribution_payload["team_pts_ot_total"],
        "contribution_game_date": contribution_payload["game_date"],
        "context_player_id": context_payload["player_id"],
        "context_player_name": context_payload["player_name"],
        "context_team_abbr": context_payload["team_abbr"],
        "context_team_name": context_payload["team_name"],
        "context_position": context_payload["position"],
        "context_height": context_payload["height"],
        "context_weight": context_payload["weight"],
        "context_roster_status": context_payload["roster_status"],
        "context_season_exp": context_payload["season_exp"],
        "context_draft_year": context_payload["draft_year"],
        "context_draft_round": context_payload["draft_round"],
        "context_draft_number": context_payload["draft_number"],
        "freshness_ts": pd.to_datetime(
            freshness_ts or created_at, utc=True
        ).isoformat(),
        "source_run_id": source_run_id,
    }


def upsert_analysis_snapshot(
    bq_client: bigquery.Client,
    table_id: str,
    record: Dict[str, Any],
) -> None:
    """Upsert a deterministic analysis snapshot keyed by snapshot_id."""
    create_analysis_snapshot_table(bq_client, table_id)
    merge_sql = f"""
    MERGE `{table_id}` T
    USING (
      SELECT
        @snapshot_id AS snapshot_id,
        @snapshot_date AS snapshot_date,
        @created_at_utc AS created_at_utc,
        @season AS season,
        @headline AS headline,
        @dek AS dek,
        @body AS body,
        @trend_player AS trend_player,
        @trend_stat AS trend_stat,
        @trend_delta AS trend_delta,
        @contribution_player_id AS contribution_player_id,
        @contribution_player_name AS contribution_player_name,
        @contribution_team_abbr AS contribution_team_abbr,
        @contribution_opponent_abbr AS contribution_opponent_abbr,
        @contribution_matchup AS contribution_matchup,
        @contribution_player_pts AS contribution_player_pts,
        @contribution_team_pts AS contribution_team_pts,
        @contribution_opponent_team_pts AS contribution_opponent_team_pts,
        @contribution_player_points_share_of_team AS contribution_player_points_share_of_team,
        @contribution_player_points_share_of_game AS contribution_player_points_share_of_game,
        @contribution_scoring_margin AS contribution_scoring_margin,
        @contribution_team_pts_qtr1 AS contribution_team_pts_qtr1,
        @contribution_team_pts_qtr2 AS contribution_team_pts_qtr2,
        @contribution_team_pts_qtr3 AS contribution_team_pts_qtr3,
        @contribution_team_pts_qtr4 AS contribution_team_pts_qtr4,
        @contribution_team_pts_ot_total AS contribution_team_pts_ot_total,
        @contribution_game_date AS contribution_game_date,
        @context_player_id AS context_player_id,
        @context_player_name AS context_player_name,
        @context_team_abbr AS context_team_abbr,
        @context_team_name AS context_team_name,
        @context_position AS context_position,
        @context_height AS context_height,
        @context_weight AS context_weight,
        @context_roster_status AS context_roster_status,
        @context_season_exp AS context_season_exp,
        @context_draft_year AS context_draft_year,
        @context_draft_round AS context_draft_round,
        @context_draft_number AS context_draft_number,
        @freshness_ts AS freshness_ts,
        @source_run_id AS source_run_id
    ) S
    ON T.snapshot_id = S.snapshot_id
    WHEN MATCHED THEN
      UPDATE SET
        snapshot_date = S.snapshot_date,
        created_at_utc = S.created_at_utc,
        season = S.season,
        headline = S.headline,
        dek = S.dek,
        body = S.body,
        trend_player = S.trend_player,
        trend_stat = S.trend_stat,
        trend_delta = S.trend_delta,
        contribution_player_id = S.contribution_player_id,
        contribution_player_name = S.contribution_player_name,
        contribution_team_abbr = S.contribution_team_abbr,
        contribution_opponent_abbr = S.contribution_opponent_abbr,
        contribution_matchup = S.contribution_matchup,
        contribution_player_pts = S.contribution_player_pts,
        contribution_team_pts = S.contribution_team_pts,
        contribution_opponent_team_pts = S.contribution_opponent_team_pts,
        contribution_player_points_share_of_team = S.contribution_player_points_share_of_team,
        contribution_player_points_share_of_game = S.contribution_player_points_share_of_game,
        contribution_scoring_margin = S.contribution_scoring_margin,
        contribution_team_pts_qtr1 = S.contribution_team_pts_qtr1,
        contribution_team_pts_qtr2 = S.contribution_team_pts_qtr2,
        contribution_team_pts_qtr3 = S.contribution_team_pts_qtr3,
        contribution_team_pts_qtr4 = S.contribution_team_pts_qtr4,
        contribution_team_pts_ot_total = S.contribution_team_pts_ot_total,
        contribution_game_date = S.contribution_game_date,
        context_player_id = S.context_player_id,
        context_player_name = S.context_player_name,
        context_team_abbr = S.context_team_abbr,
        context_team_name = S.context_team_name,
        context_position = S.context_position,
        context_height = S.context_height,
        context_weight = S.context_weight,
        context_roster_status = S.context_roster_status,
        context_season_exp = S.context_season_exp,
        context_draft_year = S.context_draft_year,
        context_draft_round = S.context_draft_round,
        context_draft_number = S.context_draft_number,
        freshness_ts = S.freshness_ts,
        source_run_id = S.source_run_id
    WHEN NOT MATCHED THEN
      INSERT (
        snapshot_id, snapshot_date, created_at_utc, season, headline, dek, body,
        trend_player, trend_stat, trend_delta, contribution_player_id,
        contribution_player_name, contribution_team_abbr, contribution_opponent_abbr,
        contribution_matchup, contribution_player_pts, contribution_team_pts,
        contribution_opponent_team_pts, contribution_player_points_share_of_team,
        contribution_player_points_share_of_game, contribution_scoring_margin,
        contribution_team_pts_qtr1, contribution_team_pts_qtr2,
        contribution_team_pts_qtr3, contribution_team_pts_qtr4,
        contribution_team_pts_ot_total, contribution_game_date, context_player_id,
        context_player_name, context_team_abbr, context_team_name, context_position,
        context_height, context_weight, context_roster_status, context_season_exp,
        context_draft_year, context_draft_round, context_draft_number,
        freshness_ts, source_run_id
      )
      VALUES (
        S.snapshot_id, S.snapshot_date, S.created_at_utc, S.season, S.headline, S.dek, S.body,
        S.trend_player, S.trend_stat, S.trend_delta, S.contribution_player_id,
        S.contribution_player_name, S.contribution_team_abbr, S.contribution_opponent_abbr,
        S.contribution_matchup, S.contribution_player_pts, S.contribution_team_pts,
        S.contribution_opponent_team_pts, S.contribution_player_points_share_of_team,
        S.contribution_player_points_share_of_game, S.contribution_scoring_margin,
        S.contribution_team_pts_qtr1, S.contribution_team_pts_qtr2,
        S.contribution_team_pts_qtr3, S.contribution_team_pts_qtr4,
        S.contribution_team_pts_ot_total, S.contribution_game_date, S.context_player_id,
        S.context_player_name, S.context_team_abbr, S.context_team_name, S.context_position,
        S.context_height, S.context_weight, S.context_roster_status, S.context_season_exp,
        S.context_draft_year, S.context_draft_round, S.context_draft_number,
        S.freshness_ts, S.source_run_id
      )
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(
                "snapshot_id", "STRING", record["snapshot_id"]
            ),
            bigquery.ScalarQueryParameter(
                "snapshot_date", "DATE", record["snapshot_date"]
            ),
            bigquery.ScalarQueryParameter(
                "created_at_utc", "TIMESTAMP", record["created_at_utc"]
            ),
            bigquery.ScalarQueryParameter("season", "STRING", record["season"]),
            bigquery.ScalarQueryParameter("headline", "STRING", record["headline"]),
            bigquery.ScalarQueryParameter("dek", "STRING", record["dek"]),
            bigquery.ScalarQueryParameter("body", "STRING", record["body"]),
            bigquery.ScalarQueryParameter(
                "trend_player", "STRING", record["trend_player"]
            ),
            bigquery.ScalarQueryParameter("trend_stat", "STRING", record["trend_stat"]),
            bigquery.ScalarQueryParameter(
                "trend_delta", "FLOAT64", record["trend_delta"]
            ),
            bigquery.ScalarQueryParameter(
                "contribution_player_id", "INT64", record["contribution_player_id"]
            ),
            bigquery.ScalarQueryParameter(
                "contribution_player_name",
                "STRING",
                record["contribution_player_name"],
            ),
            bigquery.ScalarQueryParameter(
                "contribution_team_abbr", "STRING", record["contribution_team_abbr"]
            ),
            bigquery.ScalarQueryParameter(
                "contribution_opponent_abbr",
                "STRING",
                record["contribution_opponent_abbr"],
            ),
            bigquery.ScalarQueryParameter(
                "contribution_matchup", "STRING", record["contribution_matchup"]
            ),
            bigquery.ScalarQueryParameter(
                "contribution_player_pts", "INT64", record["contribution_player_pts"]
            ),
            bigquery.ScalarQueryParameter(
                "contribution_team_pts", "INT64", record["contribution_team_pts"]
            ),
            bigquery.ScalarQueryParameter(
                "contribution_opponent_team_pts",
                "INT64",
                record["contribution_opponent_team_pts"],
            ),
            bigquery.ScalarQueryParameter(
                "contribution_player_points_share_of_team",
                "FLOAT64",
                record["contribution_player_points_share_of_team"],
            ),
            bigquery.ScalarQueryParameter(
                "contribution_player_points_share_of_game",
                "FLOAT64",
                record["contribution_player_points_share_of_game"],
            ),
            bigquery.ScalarQueryParameter(
                "contribution_scoring_margin",
                "INT64",
                record["contribution_scoring_margin"],
            ),
            bigquery.ScalarQueryParameter(
                "contribution_team_pts_qtr1",
                "INT64",
                record["contribution_team_pts_qtr1"],
            ),
            bigquery.ScalarQueryParameter(
                "contribution_team_pts_qtr2",
                "INT64",
                record["contribution_team_pts_qtr2"],
            ),
            bigquery.ScalarQueryParameter(
                "contribution_team_pts_qtr3",
                "INT64",
                record["contribution_team_pts_qtr3"],
            ),
            bigquery.ScalarQueryParameter(
                "contribution_team_pts_qtr4",
                "INT64",
                record["contribution_team_pts_qtr4"],
            ),
            bigquery.ScalarQueryParameter(
                "contribution_team_pts_ot_total",
                "INT64",
                record["contribution_team_pts_ot_total"],
            ),
            bigquery.ScalarQueryParameter(
                "contribution_game_date", "DATE", record["contribution_game_date"]
            ),
            bigquery.ScalarQueryParameter(
                "context_player_id", "INT64", record["context_player_id"]
            ),
            bigquery.ScalarQueryParameter(
                "context_player_name", "STRING", record["context_player_name"]
            ),
            bigquery.ScalarQueryParameter(
                "context_team_abbr", "STRING", record["context_team_abbr"]
            ),
            bigquery.ScalarQueryParameter(
                "context_team_name", "STRING", record["context_team_name"]
            ),
            bigquery.ScalarQueryParameter(
                "context_position", "STRING", record["context_position"]
            ),
            bigquery.ScalarQueryParameter(
                "context_height", "STRING", record["context_height"]
            ),
            bigquery.ScalarQueryParameter(
                "context_weight", "INT64", record["context_weight"]
            ),
            bigquery.ScalarQueryParameter(
                "context_roster_status", "BOOL", record["context_roster_status"]
            ),
            bigquery.ScalarQueryParameter(
                "context_season_exp", "INT64", record["context_season_exp"]
            ),
            bigquery.ScalarQueryParameter(
                "context_draft_year", "STRING", record["context_draft_year"]
            ),
            bigquery.ScalarQueryParameter(
                "context_draft_round", "STRING", record["context_draft_round"]
            ),
            bigquery.ScalarQueryParameter(
                "context_draft_number", "STRING", record["context_draft_number"]
            ),
            bigquery.ScalarQueryParameter(
                "freshness_ts", "TIMESTAMP", record["freshness_ts"]
            ),
            bigquery.ScalarQueryParameter(
                "source_run_id", "STRING", record["source_run_id"]
            ),
        ]
    )
    bq_client.query(merge_sql, job_config=job_config).result()


SIMILARITY_FEATURE_COLUMNS = [
    "season_avg_pts",
    "season_avg_reb",
    "season_avg_ast",
    "season_avg_stl",
    "season_avg_blk",
    "season_avg_fg3m",
    "season_avg_tov",
    "season_avg_min",
    "recent_pts",
    "recent_reb",
    "recent_ast",
    "recent_stl",
    "recent_blk",
    "recent_fg3m",
    "recent_tov",
    "recent_min",
    "recent_points_share_of_team",
    "recent_points_share_of_game",
    "minutes_delta_vs_season",
]

SIMILARITY_TRAIT_LABELS = {
    "season_avg_pts": "scoring volume",
    "season_avg_reb": "rebounding",
    "season_avg_ast": "playmaking",
    "season_avg_stl": "steals pressure",
    "season_avg_blk": "rim protection",
    "season_avg_fg3m": "three-point volume",
    "season_avg_min": "minutes load",
    "recent_points_share_of_team": "usage share",
    "recent_points_share_of_game": "game scoring share",
    "minutes_delta_vs_season": "minutes trend",
}

ALLOWED_ARCHETYPE_LABELS = {
    "Primary Creator",
    "Scoring Guard",
    "Two-Way Wing",
    "Connector Wing",
    "Stretch Big",
    "Interior Big",
}


def _coerce_similarity_feature_frame(feature_df: pd.DataFrame) -> pd.DataFrame:
    """Normalize dtypes for player similarity feature engineering."""
    if feature_df.empty:
        return feature_df.copy()

    working = feature_df.copy()
    working["player_id"] = pd.to_numeric(working["player_id"], errors="coerce")
    working = working.dropna(subset=["player_id"]).copy()
    working["player_id"] = working["player_id"].astype(int)
    working["games_sampled"] = (
        pd.to_numeric(working.get("games_sampled"), errors="coerce")
        .fillna(0)
        .astype(int)
    )
    working["season"] = working["season"].astype("string")
    working["player_name"] = working["player_name"].fillna("").astype("string")
    working["team_abbr"] = working["team_abbr"].fillna("").astype("string")
    if "position" not in working:
        working["position"] = ""
    if "sample_status" not in working:
        working["sample_status"] = "insufficient_sample"
    working["position"] = working["position"].fillna("").astype("string")
    working["sample_status"] = (
        working["sample_status"].fillna("insufficient_sample").astype("string")
    )
    working["as_of_date"] = pd.to_datetime(
        working["as_of_date"], errors="coerce"
    ).dt.date

    for column in SIMILARITY_FEATURE_COLUMNS:
        working[column] = pd.to_numeric(working.get(column), errors="coerce")

    return working


def _rank_similarity_traits(
    values: Dict[str, float],
    *,
    limit: int = 3,
    positive_only: bool = False,
    negative_only: bool = False,
) -> List[str]:
    ranked: List[Tuple[str, float]] = []
    for feature_name, trait_label in SIMILARITY_TRAIT_LABELS.items():
        raw_value = values.get(feature_name)
        if raw_value in (None, ""):
            continue
        score = float(raw_value)
        if positive_only and score <= 0:
            continue
        if negative_only and score >= 0:
            continue
        ranked.append((trait_label, score))

    if negative_only:
        ranked.sort(key=lambda item: item[1])
    elif positive_only:
        ranked.sort(key=lambda item: item[1], reverse=True)
    else:
        ranked.sort(key=lambda item: abs(item[1]), reverse=True)

    return [label for label, _ in ranked[:limit]]


def _label_cluster(center_values: Dict[str, float]) -> str:
    """Map a cluster center to a stable human-readable archetype label."""
    assists = float(center_values.get("season_avg_ast", 0.0))
    points = float(center_values.get("season_avg_pts", 0.0))
    rebounds = float(center_values.get("season_avg_reb", 0.0))
    steals = float(center_values.get("season_avg_stl", 0.0))
    blocks = float(center_values.get("season_avg_blk", 0.0))
    threes = float(center_values.get("season_avg_fg3m", 0.0))
    usage = float(center_values.get("recent_points_share_of_team", 0.0))

    if rebounds >= 0.7 and blocks >= 0.5 and threes >= 0.15:
        return "Stretch Big"
    if rebounds >= 0.85 and blocks >= 0.45:
        return "Interior Big"
    if assists >= 0.9 and usage >= 0.35:
        return "Primary Creator"
    if points >= 0.7 and threes >= 0.45 and assists < 0.9:
        return "Scoring Guard"
    if steals >= 0.2 and threes >= 0.1 and rebounds >= -0.1:
        return "Two-Way Wing"
    return "Connector Wing"


def _build_cluster_summary(archetype_label: str, top_traits: List[str]) -> str:
    if not top_traits:
        return archetype_label
    return f"{archetype_label} driven by {', '.join(top_traits)}."


def _validate_similarity_output_frames(
    features_df: pd.DataFrame, archetypes_df: pd.DataFrame
) -> None:
    """Enforce minimum output contracts for similarity tables."""
    if features_df.empty or archetypes_df.empty:
        raise ValueError("Similarity outputs must not be empty")

    feature_dupes = features_df.duplicated(subset=["season", "player_id"]).any()
    archetype_dupes = archetypes_df.duplicated(subset=["season", "player_id"]).any()
    if feature_dupes:
        raise ValueError(
            "Duplicate season/player rows found in player_similarity_features"
        )
    if archetype_dupes:
        raise ValueError("Duplicate season/player rows found in player_archetypes")

    if features_df["player_id"].isna().any() or archetypes_df["player_id"].isna().any():
        raise ValueError("player_id must not be null in similarity outputs")

    invalid_labels = (
        set(archetypes_df["archetype_label"].dropna()) - ALLOWED_ARCHETYPE_LABELS
    )
    if invalid_labels:
        raise ValueError(
            f"Unexpected archetype labels detected: {sorted(invalid_labels)}"
        )


def build_player_similarity_outputs(
    feature_df: pd.DataFrame,
    *,
    cluster_count: int = 6,
) -> Dict[str, pd.DataFrame]:
    """Cluster players into archetypes and publish normalized similarity vectors."""
    import numpy as np
    from sklearn.cluster import KMeans
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import StandardScaler

    working = _coerce_similarity_feature_frame(feature_df)
    if working.empty:
        raise ValueError("Cannot build similarity outputs from an empty feature frame")

    modeling = working[working["sample_status"] != "insufficient_sample"].copy()
    if modeling.empty:
        raise ValueError("No players meet the minimum sample threshold for similarity")

    imputer = SimpleImputer(strategy="median")
    imputed_values = imputer.fit_transform(modeling[SIMILARITY_FEATURE_COLUMNS])
    scaler = StandardScaler()
    scaled_values = scaler.fit_transform(imputed_values)

    effective_clusters = max(1, min(int(cluster_count), len(modeling)))
    kmeans = KMeans(n_clusters=effective_clusters, n_init=20, random_state=42)
    cluster_ids = kmeans.fit_predict(scaled_values)
    distances = np.linalg.norm(
        scaled_values - kmeans.cluster_centers_[cluster_ids], axis=1
    )

    modeling = modeling.reset_index(drop=True)
    modeling["cluster_index"] = cluster_ids

    cluster_summaries: Dict[int, Dict[str, Any]] = {}
    for cluster_index in sorted(modeling["cluster_index"].unique()):
        center = {
            feature_name: float(value)
            for feature_name, value in zip(
                SIMILARITY_FEATURE_COLUMNS,
                kmeans.cluster_centers_[cluster_index],
            )
        }
        top_traits = _rank_similarity_traits(center, limit=3, positive_only=True)
        archetype_label = _label_cluster(center)
        cluster_summaries[int(cluster_index)] = {
            "archetype_id": f"cluster_{int(cluster_index)}",
            "archetype_label": archetype_label,
            "top_traits": ", ".join(top_traits),
            "archetype_summary": _build_cluster_summary(archetype_label, top_traits),
        }

    confidence_by_row: List[float] = []
    for row_index, cluster_index in enumerate(cluster_ids):
        cluster_mask = cluster_ids == cluster_index
        cluster_distances = distances[cluster_mask]
        max_distance = float(cluster_distances.max()) if len(cluster_distances) else 0.0
        if max_distance <= 1e-9:
            confidence = 1.0
        else:
            confidence = 1.0 - float(distances[row_index]) / max_distance
        confidence_by_row.append(round(max(0.0, min(confidence, 1.0)), 4))

    normalized_columns = {
        f"norm_{feature_name}": scaled_values[:, index]
        for index, feature_name in enumerate(SIMILARITY_FEATURE_COLUMNS)
    }
    modeling = modeling.assign(**normalized_columns)
    modeling["cluster_confidence"] = confidence_by_row

    player_top_traits: List[str] = []
    player_bottom_traits: List[str] = []
    archetype_ids: List[str] = []
    archetype_labels: List[str] = []
    archetype_summaries: List[str] = []
    for _, row in modeling.iterrows():
        normalized_values = {
            feature_name: float(row[f"norm_{feature_name}"])
            for feature_name in SIMILARITY_FEATURE_COLUMNS
        }
        player_top_traits.append(
            ", ".join(
                _rank_similarity_traits(normalized_values, limit=3, positive_only=True)
            )
        )
        player_bottom_traits.append(
            ", ".join(
                _rank_similarity_traits(normalized_values, limit=2, negative_only=True)
            )
        )
        cluster_summary = cluster_summaries[int(row["cluster_index"])]
        archetype_ids.append(cluster_summary["archetype_id"])
        archetype_labels.append(cluster_summary["archetype_label"])
        archetype_summaries.append(cluster_summary["archetype_summary"])

    modeling["top_traits"] = player_top_traits
    modeling["contrasting_traits"] = player_bottom_traits
    modeling["archetype_id"] = archetype_ids
    modeling["archetype_label"] = archetype_labels
    modeling["archetype_summary"] = archetype_summaries

    features_df = modeling[
        [
            "season",
            "as_of_date",
            "player_id",
            "player_name",
            "team_abbr",
            "position",
            "games_sampled",
            "sample_status",
            "archetype_id",
            "archetype_label",
            "cluster_confidence",
            "top_traits",
            "contrasting_traits",
            "archetype_summary",
            *SIMILARITY_FEATURE_COLUMNS,
            *[f"norm_{feature_name}" for feature_name in SIMILARITY_FEATURE_COLUMNS],
        ]
    ].copy()

    archetypes_df = modeling[
        [
            "season",
            "as_of_date",
            "player_id",
            "player_name",
            "team_abbr",
            "position",
            "games_sampled",
            "sample_status",
            "archetype_id",
            "archetype_label",
            "cluster_confidence",
            "top_traits",
            "archetype_summary",
        ]
    ].copy()

    _validate_similarity_output_frames(features_df, archetypes_df)
    return {"features": features_df, "archetypes": archetypes_df}


def _player_similarity_feature_schema() -> List[bigquery.SchemaField]:
    fields = [
        bigquery.SchemaField("season", "STRING"),
        bigquery.SchemaField("as_of_date", "DATE"),
        bigquery.SchemaField("player_id", "INT64"),
        bigquery.SchemaField("player_name", "STRING"),
        bigquery.SchemaField("team_abbr", "STRING"),
        bigquery.SchemaField("position", "STRING"),
        bigquery.SchemaField("games_sampled", "INT64"),
        bigquery.SchemaField("sample_status", "STRING"),
        bigquery.SchemaField("archetype_id", "STRING"),
        bigquery.SchemaField("archetype_label", "STRING"),
        bigquery.SchemaField("cluster_confidence", "FLOAT64"),
        bigquery.SchemaField("top_traits", "STRING"),
        bigquery.SchemaField("contrasting_traits", "STRING"),
        bigquery.SchemaField("archetype_summary", "STRING"),
    ]
    for feature_name in SIMILARITY_FEATURE_COLUMNS:
        fields.append(bigquery.SchemaField(feature_name, "FLOAT64"))
    for feature_name in SIMILARITY_FEATURE_COLUMNS:
        fields.append(bigquery.SchemaField(f"norm_{feature_name}", "FLOAT64"))
    return fields


def _player_archetype_schema() -> List[bigquery.SchemaField]:
    return [
        bigquery.SchemaField("season", "STRING"),
        bigquery.SchemaField("as_of_date", "DATE"),
        bigquery.SchemaField("player_id", "INT64"),
        bigquery.SchemaField("player_name", "STRING"),
        bigquery.SchemaField("team_abbr", "STRING"),
        bigquery.SchemaField("position", "STRING"),
        bigquery.SchemaField("games_sampled", "INT64"),
        bigquery.SchemaField("sample_status", "STRING"),
        bigquery.SchemaField("archetype_id", "STRING"),
        bigquery.SchemaField("archetype_label", "STRING"),
        bigquery.SchemaField("cluster_confidence", "FLOAT64"),
        bigquery.SchemaField("top_traits", "STRING"),
        bigquery.SchemaField("archetype_summary", "STRING"),
    ]


def write_player_similarity_tables(
    bq_client: bigquery.Client,
    *,
    features_table_id: str,
    archetypes_table_id: str,
    features_df: pd.DataFrame,
    archetypes_df: pd.DataFrame,
) -> None:
    """Write similarity feature and archetype tables to BigQuery."""
    seasons = sorted({str(value) for value in features_df["season"].dropna().unique()})
    feature_table = bigquery.Table(
        features_table_id, schema=_player_similarity_feature_schema()
    )
    archetype_table = bigquery.Table(
        archetypes_table_id, schema=_player_archetype_schema()
    )
    bq_client.create_table(feature_table, exists_ok=True)
    bq_client.create_table(archetype_table, exists_ok=True)

    delete_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("seasons", "STRING", seasons),
        ]
    )
    bq_client.query(
        f"DELETE FROM `{features_table_id}` WHERE season IN UNNEST(@seasons)",
        job_config=delete_config,
    ).result()
    bq_client.query(
        f"DELETE FROM `{archetypes_table_id}` WHERE season IN UNNEST(@seasons)",
        job_config=delete_config,
    ).result()

    feature_job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        schema=_player_similarity_feature_schema(),
    )
    archetype_job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        schema=_player_archetype_schema(),
    )

    bq_client.load_table_from_dataframe(
        features_df, features_table_id, job_config=feature_job_config
    ).result()
    bq_client.load_table_from_dataframe(
        archetypes_df, archetypes_table_id, job_config=archetype_job_config
    ).result()
