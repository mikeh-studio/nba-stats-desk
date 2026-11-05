# Manual integration runners

These scripts exercise real Airflow or cloud clients with synthetic inputs. They
are deliberately outside pytest discovery. They do not run in ordinary CI and
must be invoked explicitly in an appropriately configured environment. Cloud runs
can allocate resources and incur usage; a successful offline unit test does not
validate them.

| Runner | Integration |
| --- | --- |
| `espn_cloud_scenarios.py` | ESPN capture, correction, replay and rollback in GCS/BigQuery |
| `espn_identity_cloud_scenarios.py` | Reviewed identity enforcement in cloud publication |
| `espn_layer_cloud_scenarios.py` | Independent layers and delayed corrections in BigQuery |
| `espn_only_cloud_scenarios.py` | ESPN-only publication and independent verification |
| `gamebook_airflow_scenarios.py` | Real Airflow retries/mapping with synthetic HTTP and local SQLite |
| `gamebook_automation_cloud_scenarios.py` | Unattended gamebook worker with cloud storage |
| `gamebook_cloud_scenarios.py` | Gamebook corrections and transaction rollback in cloud storage |

Cloud runners take `--output reports/UNIQUE_RUN`; use `--help` to inspect their
arguments before an authorized run. They require their existing isolated development
warehouse/storage configuration and credentials. Airflow's runner requires Airflow
and a fresh `AIRFLOW_HOME` beneath `reports/`; its worker interpreter can be set
with `GAMEBOOK_PYTHON`. It also uses an internal `--attempt` subprocess mode.
Results, logs and captured data remain under ignored `reports/` paths.

Old `tests/*_scenarios.py` commands forward here for compatibility. The collected
unit tests and their shared helpers, including `gamebook_storage_scenarios.py`,
remain in `tests/`. Moving these manual entry points does not make them automatic
tests or change their source, authority, isolation or publication requirements.
