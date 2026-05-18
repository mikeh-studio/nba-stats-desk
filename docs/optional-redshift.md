# Redshift Secondary Warehouse

Redshift Serverless is an optional secondary warehouse for cross-cloud
portfolio and learning work. BigQuery remains the system of record.

Enable the branch with:

```env
ENABLE_REDSHIFT=true
```

## Flow

```text
BigQuery bronze
  -> GCS Parquet
  -> S3
  -> Redshift COPY
  -> dbt Redshift models
```

The DAG appends Redshift sync tasks after BigQuery bronze merges when the branch
is enabled.

## Infrastructure

AWS infrastructure lives in `infra/terraform-aws/`.

Local Terraform state and `*.tfvars` are ignored and should not be committed.

## Local Requirements

Install the Redshift dbt adapter before local Redshift validation:

```bash
pip install dbt-redshift
```

Set Redshift variables in `.env` using `.env.example` as the template.

## Validation

Parse Redshift-compatible dbt models:

```bash
dbt parse --project-dir . --profiles-dir dbt/profiles --target redshift
```

Run the recommended compatibility build:

```bash
dbt build --project-dir . --profiles-dir dbt/profiles --target redshift \
  --select path:dbt/models/silver
```

This requires working Redshift credentials, network access, and the
`dbt-redshift` adapter.
