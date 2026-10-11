resource "google_storage_bucket" "landing" {
  name                        = var.gcs_bucket_name
  location                    = var.bucket_location
  uniform_bucket_level_access = true
}

resource "google_bigquery_dataset" "bronze" {
  dataset_id = var.bronze_dataset
  location   = var.bucket_location
}

resource "google_bigquery_dataset" "silver" {
  dataset_id = var.silver_dataset
  location   = var.bucket_location
}

resource "google_bigquery_dataset" "gold" {
  dataset_id = var.gold_dataset
  location   = var.bucket_location
}

resource "google_bigquery_dataset" "agent" {
  dataset_id = var.agent_dataset
  location   = var.bucket_location
}

resource "google_bigquery_dataset" "metadata" {
  dataset_id = var.metadata_dataset
  location   = var.bucket_location
}

resource "google_service_account" "pipeline" {
  account_id   = var.service_account_id
  display_name = "NBA pipeline runtime"
}

resource "google_project_iam_member" "bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.pipeline.email}"
}

# Scope transformation/publication writes to the five managed datasets.
resource "google_bigquery_dataset_iam_member" "pipeline_editor" {
  for_each = {
    bronze   = google_bigquery_dataset.bronze.dataset_id
    silver   = google_bigquery_dataset.silver.dataset_id
    gold     = google_bigquery_dataset.gold.dataset_id
    agent    = google_bigquery_dataset.agent.dataset_id
    metadata = google_bigquery_dataset.metadata.dataset_id
  }

  project    = var.project_id
  dataset_id = each.value
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.pipeline.email}"
}

# Supports landing overwrites and export reads, without object ACL administration.
resource "google_storage_bucket_iam_member" "pipeline_objects" {
  bucket = google_storage_bucket.landing.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.pipeline.email}"
}
