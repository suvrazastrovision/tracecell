resource "google_project_service" "warehouse_apis" {
  for_each           = toset(["bigquery.googleapis.com", "cloudresourcemanager.googleapis.com"])
  project            = "pbmc-okf-portfolio"
  service            = each.value
  disable_on_destroy = false
}

# Read the already-existing dataset without adopting or recreating it.
data "google_bigquery_dataset" "raw" {
  project    = "pbmc-okf-portfolio"
  dataset_id = "pbmc_raw"
}

resource "google_service_account" "warehouse_runner" {
  project      = "pbmc-okf-portfolio"
  account_id   = "pbmc-warehouse-runner"
  display_name = "PBMC warehouse load job"
  description  = "Loads verified GCS releases into the existing PBMC raw dataset."

  depends_on = [google_project_service.iam]
}

resource "google_project_iam_member" "warehouse_jobs" {
  project = "pbmc-okf-portfolio"
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.warehouse_runner.email}"

  depends_on = [google_project_service.warehouse_apis]
}

resource "google_bigquery_dataset_iam_member" "warehouse_raw" {
  project    = data.google_bigquery_dataset.raw.project
  dataset_id = data.google_bigquery_dataset.raw.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.warehouse_runner.email}"

  depends_on = [google_project_service.warehouse_apis]
}

resource "google_storage_bucket_iam_member" "warehouse_read" {
  bucket = google_storage_bucket.pipeline_data.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.warehouse_runner.email}"

  condition {
    title = "warehouse-read-releases-and-audits"
    expression = join(" || ", [
      "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.pipeline_data.name}/objects/processed/releases/')",
      "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.pipeline_data.name}/objects/runs/warehouse/')",
      "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.pipeline_data.name}/objects/runs/cloud_warehouse/')",
    ])
  }
}

resource "google_storage_bucket_iam_member" "warehouse_audits" {
  bucket = google_storage_bucket.pipeline_data.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.warehouse_runner.email}"

  condition {
    title = "warehouse-create-audits-only"
    expression = join(" || ", [
      "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.pipeline_data.name}/objects/runs/warehouse/')",
      "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.pipeline_data.name}/objects/runs/cloud_warehouse/')",
    ])
  }
}

variable "warehouse_release_id" {
  type        = string
  description = "Explicit committed QC release loaded by the standalone warehouse job."
  default     = "71a4bb4d026366b644c4aaf8bd4b29d8f8f87fe1701ba2e8d6a88ba164b83f7d"

  validation {
    condition     = can(regex("^[a-f0-9]{64}$", var.warehouse_release_id))
    error_message = "warehouse_release_id must be a full lowercase SHA-256 release ID."
  }
}
