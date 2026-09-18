# Historical seasons

The app supports `2025-26`, `2024-25`, and `2023-24`. Select a season in the
navigation bar or use `?season=2023-24` on a page or API URL. The selection is
preserved in navigation and API requests. Historical Ask conversations and
player-search caches are isolated from the current season.

Each archive uses its own datasets:

| Season | Bronze | Silver | Gold | Agent | Metadata |
| --- | --- | --- | --- | --- | --- |
| 2023-24 | nba_bronze_2023_24 | nba_silver_2023_24 | nba_gold_2023_24 | nba_agent_2023_24 | nba_metadata_2023_24 |
| 2024-25 | nba_bronze_2024_25 | nba_silver_2024_25 | nba_gold_2024_25 | nba_agent_2024_25 | nba_metadata_2024_25 |

Configured dataset base names receive the same suffix. Current-season datasets
and the scheduled Airflow DAG retain their existing names and behavior. A
historical dbt build uses one season and the final observed game date, keeping
rankings and recent-game calculations within that season.

## Backfill

Run with the environment that provides `nba_api`, BigQuery, dbt, and pandas:

```bash
.venv-airflow/bin/python scripts/backfill_historical_seasons.py \
  --directory reports/historical-seasons
```

This fetches regular-season and playoff player logs, independent team logs,
shot-location profiles, and daily official injury reports using the legacy
`05PM` archive filenames (the exact timestamp is read from each PDF).
League-wide queries include players who are no longer active. It validates both requested seasons before any warehouse write.

To publish the validated inputs and build the archives:

```bash
.venv-airflow/bin/python scripts/backfill_historical_seasons.py \
  --directory reports/historical-seasons --skip-extract --publish \
  --project YOUR_PROJECT --bucket YOUR_BUCKET
```

Use `--seasons 2023-24` to process one archive. The command refuses the current
season. The app's BigQuery principal needs read access to the new gold, agent,
and metadata datasets, plus its existing query-job permission.

Validation checks the production column/type/key contracts with the exact
historical date bounds; rejected rows block writes. Coverage checks require
1,230 regular-season games, 82 team appearances per team, and 15 playoff series
with a four-win series winner. Every game must have two team rows and player
scoring totals and dates must match independent team logs. Shot-profile player
coverage must match the corresponding game logs.

GCS inputs are stored at content-addressed paths under
`nba_data/<season>/historical_backfill/`. Reports record object generations,
SHA-256 fingerprints, row counts, source checks, and validation results. Unmatched injury-player
names are retained with their source name and a NULL player ID, as a contract
warning; they are not assigned to a guessed player.
Bronze writes use `WRITE_EMPTY`; retries require the same source fingerprint
and cannot overwrite a different archive by default. A retry can resume a matching empty
table after a failed initial load. To add newly validated sources or correct
this command's own historical inputs, `--replace-owned-archive` first copies
the affected tables to timestamped backups, then replaces them. It refuses
unowned tables and empty replacements. Previous local reports are retained
under `report_history/`. dbt and similarity outputs rebuild only the
selected historical datasets. Normal ingestion watermarks and production audit
records are not changed.

After successful dbt and similarity builds, the archive publishes its own
`historical_backfill_manifest` table. Failed builds retain their local report
and do not publish a success manifest. dbt job creation, execution, and the
build process have explicit time bounds.

## Coverage limits

- Coverage follows the NBA API's `Regular Season` and `Playoffs` categories.
- Team final scores are reconciled against official team logs. Quarter and
  overtime-period scores are unavailable in these league-wide extracts and
  remain NULL, including the derived overtime total.
- Schedule rows describe observed completed games. Neutral-site games whose
  logs mark both teams away use official game-summary home/visitor IDs. Player reference rows carry
  the season's last observed name/team; biography and physical measurements
  unavailable in these extracts remain NULL.
- Injury history uses one daily archive file and is never inferred from missed appearances.
  A supplied `<season>_injuries.parquet` must satisfy the injury contract.
  No injury rows means unavailable evidence, not a healthy player.
  `<season>_injury_source_checks.json` records every attempted daily archive.
  Download and parsing failures are recorded per day with `status: error`;
  other days continue. If no reports are usable, validation records missing
  injury coverage. Transport/HTTP failures receive up to three attempts;
  malformed headers stop after one. To retry failed days, remove only the
  combined `<season>_injuries.parquet` and rerun extraction; successful daily
  caches are reused. Local storage failures still abort because audit evidence
  cannot be reliably saved.
  Later intraday status changes are outside this collection; discrepancies
  between an earlier Out report and actual appearances remain warning rows.
- Historical pages show archive status and do not expect daily refreshes.
  Similarity outputs can have less physical-profile context than the current
  season because historical biography fields are incomplete.

The backfill report is the evidence for actual coverage. A successful local
app check does not deploy the app or grant a production service account access.

## Metric semantics

The [governed semantic contract](semantic-contract.md) describes implemented
scope, aggregation, qualification, and evidence boundaries. Use the
[evaluation workflow](evaluation.md) to validate a frozen archive. Actual run
counts, source-capture audits, and warehouse job records belong in local reports.
