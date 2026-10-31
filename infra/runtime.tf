resource "google_project_service" "iam" {
  project            = "pbmc-okf-portfolio"
  service            = "iam.googleapis.com"
  disable_on_destroy = false
}

resource "google_service_account" "qc_runner" {
  project      = "pbmc-okf-portfolio"
  account_id   = "pbmc-qc-runner"
  display_name = "PBMC QC Cloud Run job"
  description  = "Runs PBMC QC and publishes verified releases to the pipeline data bucket."

  depends_on = [google_project_service.iam]
}

# Bucket-scoped grants let the job verify and create objects while preserving
# existing releases. Do not grant access to the Terraform state bucket.
resource "google_storage_bucket_iam_member" "qc_read" {
  bucket = google_storage_bucket.pipeline_data.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.qc_runner.email}"
}

resource "google_storage_bucket_iam_member" "qc_create" {
  bucket = google_storage_bucket.pipeline_data.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.qc_runner.email}"
}

output "qc_runner_service_account" {
  value = google_service_account.qc_runner.email
}
