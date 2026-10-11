# The mock provider makes both apply and teardown local-only; no cloud calls.
mock_provider "google" {}

variables {
  project_id      = "iam-policy-test"
  gcs_bucket_name = "iam-policy-test-landing"
}

run "pipeline_scope" {
  command = apply

  assert {
    condition = (
      google_project_iam_member.bq_job_user.role == "roles/bigquery.jobUser" &&
      google_project_iam_member.bq_job_user.project == var.project_id &&
      google_project_iam_member.bq_job_user.member == "serviceAccount:${google_service_account.pipeline.email}"
    )
    error_message = "Only job submission needs project scope."
  }

  assert {
    condition = (
      toset(keys(google_bigquery_dataset_iam_member.pipeline_editor)) == toset(["bronze", "silver", "gold", "agent", "metadata"]) &&
      toset([for grant in google_bigquery_dataset_iam_member.pipeline_editor : grant.dataset_id]) == toset([
        var.bronze_dataset, var.silver_dataset, var.gold_dataset, var.agent_dataset, var.metadata_dataset
      ]) &&
      alltrue([for grant in google_bigquery_dataset_iam_member.pipeline_editor :
        grant.role == "roles/bigquery.dataEditor" &&
        grant.project == var.project_id &&
        grant.member == "serviceAccount:${google_service_account.pipeline.email}"
      ])
    )
    error_message = "The pipeline must write exactly the five provisioned datasets."
  }

  assert {
    condition = (
      google_storage_bucket_iam_member.pipeline_objects.bucket == var.gcs_bucket_name &&
      google_storage_bucket_iam_member.pipeline_objects.role == "roles/storage.objectUser" &&
      google_storage_bucket_iam_member.pipeline_objects.member == "serviceAccount:${google_service_account.pipeline.email}" &&
      google_storage_bucket.landing.uniform_bucket_level_access
    )
    error_message = "Object access must remain limited to the uniform-access landing bucket."
  }
}
