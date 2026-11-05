# Manual integration runners

Run these scripts explicitly with `python tests/manual/NAME.py`. Pytest and ordinary
CI do not collect them. The former `tests/NAME.py` entry points have been removed.

- `espn_cloud_scenarios.py`: GCS/BigQuery replay, corrections and rollback.
- `espn_identity_cloud_scenarios.py`: cloud identity enforcement.
- `espn_layer_cloud_scenarios.py`: independent layers and delayed corrections.
- `espn_only_cloud_scenarios.py`: publication and independent verification.
- `gamebook_automation_cloud_scenarios.py`: unattended cloud worker.
- `gamebook_cloud_scenarios.py`: corrections and cloud transaction rollback.
- `gamebook_airflow_scenarios.py`: real Airflow retries/mapping, synthetic HTTP and SQLite.

Cloud runners accept `--help` and require `--output reports/UNIQUE_RUN`, credentials
and isolated development resources; runs can incur usage. Airflow requires a fresh
`AIRFLOW_HOME` beneath `reports/` and a worker interpreter via `GAMEBOOK_PYTHON`.
Its internal `--attempt` mode launches workers. Keep results in ignored `reports/`.
Collected unit tests and shared helpers remain in `tests/`.
