resource "google_bigquery_dataset" "analytics" {
  project                    = "pbmc-okf-portfolio"
  dataset_id                 = "pbmc_analytics_r_${var.warehouse_release_id}"
  location                   = "europe-west1"
  description                = "Release-scoped PBMC analytics built and tested by dbt."
  delete_contents_on_destroy = false

  lifecycle {
    prevent_destroy = true
  }
  depends_on = [google_project_service.warehouse_apis]
}

resource "google_service_account" "dbt_runner" {
  project      = "pbmc-okf-portfolio"
  account_id   = "pbmc-dbt-runner"
  display_name = "PBMC dbt build job"
  description  = "Reads verified raw tables and builds release-scoped analytics."
  depends_on   = [google_project_service.iam]
}

resource "google_project_iam_member" "dbt_jobs" {
  project = "pbmc-okf-portfolio"
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.dbt_runner.email}"
}

resource "google_bigquery_dataset_iam_member" "dbt_raw" {
  project    = data.google_bigquery_dataset.raw.project
  dataset_id = data.google_bigquery_dataset.raw.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.dbt_runner.email}"
}

resource "google_bigquery_dataset_iam_member" "dbt_analytics" {
  project    = google_bigquery_dataset.analytics.project
  dataset_id = google_bigquery_dataset.analytics.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.dbt_runner.email}"
}

resource "google_storage_bucket_iam_member" "dbt_read" {
  bucket = google_storage_bucket.pipeline_data.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.dbt_runner.email}"
  condition {
    title = "dbt-read-releases-and-audits"
    expression = join(" || ", [
      "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.pipeline_data.name}/objects/processed/releases/')",
      "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.pipeline_data.name}/objects/runs/dbt/')",
      "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.pipeline_data.name}/objects/runs/cloud_dbt/')",
      "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.pipeline_data.name}/objects/runs/dbt_handoff/')",
      "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.pipeline_data.name}/objects/runs/handoffs/dbt/')",
    ])
  }
}

resource "google_storage_bucket_iam_member" "dbt_artifacts" {
  bucket = google_storage_bucket.pipeline_data.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.dbt_runner.email}"
  condition {
    title = "dbt-create-artifacts-only"
    expression = join(" || ", [
      "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.pipeline_data.name}/objects/runs/dbt/')",
      "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.pipeline_data.name}/objects/runs/cloud_dbt/')",
      "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.pipeline_data.name}/objects/runs/dbt_handoff/')",
      "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.pipeline_data.name}/objects/runs/handoffs/dbt/')",
    ])
  }
}
