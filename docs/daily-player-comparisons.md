# Daily player comparison updates

The completed 2025–26 comparison inputs can be checked daily for official
statistical corrections. The local maintenance command fetches fresh regular
season and playoff league player logs and independent team logs. It includes
every recorded player, including players no longer listed as active. Players
without appearances are not assigned fabricated zero production.

```bash
# Fresh capture and validation, without warehouse writes:
.venv-app/bin/python scripts/refresh_player_comparisons.py

# Publish validated changes after the isolated build and tests:
.venv-app/bin/python scripts/refresh_player_comparisons.py --publish
```

The command loads the existing local `.env` configuration. It requires the
standard `nba_bronze`, `nba_silver`, `nba_gold` and `nba_agent` datasets and the
existing BigQuery credentials and dbt runtime. Credentials and operational
reports remain private. Reports use unique directories under
`reports/daily-player-comparisons/`; runs cannot overlap on the same host.

Before creating candidates, validation checks the complete season, player/team
totals, minutes rounding bounds, shooting identities, every previous appearance,
and the game-log source contract. Quarantined rows block the refresh and retain
their audit export. Existing verified warehouse schedule assignments preserve
neutral-site home/away designations; a newer season's CDN schedule is not used.
New games or changed date/opponent assignments require a separately verified
schedule. This job does not expand the supported season list.

An unchanged capture performs no warehouse writes or dbt build. Changed inputs
use the [existing isolated rebuild and publication](evaluation.md#split-routing-and-completed-season-warehouse-repair)
with expiring candidates, backups, dbt tests, exact fact equality, refreshed
dependent serving/similarity models, and a transactional promotion. The original
comparison snapshot must match the backed-up fact before the build begins.
All candidate contents and live baselines are checked again within publication.
Failed source reads or validation leave serving data intact. A lost connection
during publication is recorded as an unconfirmed outcome rather than falsely
claiming the old data remains; inspect the BigQuery job before any retry.

Scheduling this command is separate from validating one run. A local Codex
heartbeat can invoke the publish command daily in this chat; it requires the
local host and Codex to be available. It does not repair or restart the older
[Airflow service](local-airflow.md). The existing Airflow DAG remains the regular
incremental ingestion path, whose 2025–26 scheduled window ended in July.

The command itself makes no model calls. A Codex heartbeat uses the chat's model
to run and review it. Avoid automatic retries, source substitution, warehouse
repairs outside this command, or paid Ask evaluations. Report new failures,
recoveries, and validated changes, preserving prior receipts for comparison.

Ask derives FG%, 3P%, FT%, eFG%, TS%, turnovers per game, and points/assists per
36 from the published player-game inputs. Existing saved answers remain frozen;
new questions use current warehouse data after normal cache expiry. Win Shares
and possession metrics remain unavailable and hidden.
