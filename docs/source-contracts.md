# Source Contracts

Source contracts live under `contracts/` and run inside Airflow extract tasks
before non-empty dataframes are uploaded to GCS.

## Covered Domains

- `game_logs`
- `game_line_scores`
- `schedule`
- `player_reference`
- `injury_reports`

Contracts define required columns, expected types, business keys, season/date
scope, enum values, and domain-specific bounds.

## Severities

- `fatal`: stop the pipeline before landing the batch.
- `quarantine`: remove violating rows and continue if valid rows remain.
- `warning`: record the issue without dropping rows.

The contract layer protects the ingestion boundary. BigQuery staging DQ checks
and dbt tests still run after landing to protect warehouse state and modeled
outputs.

## Audit Output

Each non-empty extract writes a pre-validation source audit snapshot to GCS:

```text
nba_data/<season>/source_audit/raw_extract/source=<domain>/run_id=<run>/
```

Rows removed by quarantine rules are written to:

```text
nba_data/<season>/source_audit/quarantine/source=<domain>/run_id=<run>/
```

Contract outcomes are upserted into
`nba_metadata.source_contract_results` with row counts, failure counts, GCS audit
URIs, landing URI, and serialized violation details.

## Operational Notes

Game logs remain hard-gated because they are the main fact source. Supporting
domains are allowed to soft-fail after bounded retries when the pipeline can
still move core game-log data safely.
