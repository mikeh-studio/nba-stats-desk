# Pipeline IAM

The pipeline service account has BigQuery Job User on the project, Data Editor
on the five provisioned bronze, silver, gold, agent, and metadata datasets, and
Storage Object User on the landing bucket. The grant resources are additive IAM
members; they do not replace an entire IAM policy.

Dataset writes are required for bronze staging/merges, dbt transformations,
agent tables, metadata, and candidate publication/cleanup. Dataset provisioning
belongs to Terraform: the runtime reads existing datasets before any bootstrap
attempt. Missing datasets must be provisioned by an infrastructure operator;
the runtime intentionally cannot create arbitrary project datasets. Standalone
repair/backfill scripts that create additional datasets require separately
reviewed operator access and are not runtime provisioning paths.

Landing uploads can overwrite a destination during retries. Optional Redshift
sync also lists and downloads exported objects. Bucket-scoped
[Storage Object User](https://cloud.google.com/storage/docs/access-control/iam-roles)
retains these operations without object ACL administration. Object Creator alone
cannot support overwrites or reads. The bucket uses uniform bucket-level access.

## Existing deployment migration

This source change does not inspect or modify deployed IAM. Before any apply:

1. Inspect the actual Terraform backend/workspace/state, deployed effective IAM
   (including inherited grants), dataset access entries, and landing bucket.
   Confirm configured dataset/bucket names match the running DAG/dbt environment.
   Inventory every workload using the pipeline identity, including Airflow,
   Cloud Run, manual repairs, and optional export jobs. Resolve shared usage and
   any additional destinations before removing the broad grants.
2. Keep the public web/Ask query identity separate from the pipeline writer.
   The serving application reads gold, agent, and metadata. Give its dedicated
   identity only the necessary dataset Data Viewer grants and project Job User;
   do not use the pipeline credentials in the public service. These Terraform
   files do not create or switch a serving identity.
3. Check for authorized views/datasets/routines and separately managed dataset
   `access` or `google_bigquery_dataset_access` entries. Dataset IAM resources
   must not compete with those access managers; reconcile ownership before apply
   to avoid losing authorized-view grants. The repository has no such entries,
   but that does not establish the deployed state.
4. Use two reviewed deployment stages. First add the new scoped grants while
   retaining the old `google_project_iam_member.bq_data_editor` and
   `google_project_iam_member.storage_admin` blocks from the prior version in a
   temporary migration configuration. Review a plan that **adds scoped members
   without deleting either old member**, then apply only that approved stage.
   Wait for IAM propagation and inspect the resulting resource policies.
5. Validate required operations with a temporary test identity having only the
   scoped permissions and disposable data, or another explicitly approved
   isolated equivalent. Success while the old broad roles remain does not prove
   the narrower roles are sufficient. Include dataset reads, table writes/dbt
   publication, and object create/overwrite/read/list for enabled paths.
6. Return to this final configuration and review a second plan removing only
   the two old project members. Apply it in an approved change window, then
   verify effective permissions and pipeline health. Retain the prior IAM
   member definitions for an operator-reviewed rollback if required. Broad
   inherited/group grants must also be handled by their owning configuration.

Do not perform a single unreviewed apply of this diff against an existing
installation: Terraform may schedule deletions of removed project members
before additions of independent scoped members. Resource order in this file,
`depends_on` on new resources, and `create_before_destroy` do not establish the
required cross-resource migration sequence. Do not use `terraform state rm` or
`moved` blocks to pretend project and dataset/bucket grants are the same object.
New installations can provision the final configuration directly after review.

## Offline validation

```sh
terraform -chdir=infra/terraform fmt -check -recursive
terraform -chdir=infra/terraform init -backend=false
terraform -chdir=infra/terraform validate
terraform -chdir=infra/terraform test
python -m pytest tests/test_terraform_iam.py tests/test_incremental_pipeline.py -q
```

Mocked Terraform tests need Terraform 1.7 or later and make no cloud calls.
Initialization downloads the provider; no backend initialization, real plan,
apply, IAM mutation, or live pipeline validation is part of these checks.
