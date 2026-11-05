resource "google_cloud_run_v2_job" "dbt" {
  project             = "pbmc-okf-portfolio"
  name                = "pbmc-dbt-build"
  location            = "europe-west1"
  deletion_protection = false
  labels = {
    application = "pbmc-platform"
    environment = "portfolio"
  }
  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = google_service_account.dbt_runner.email
      timeout         = "1200s"
      max_retries     = 1
      containers {
        image       = var.knowledge_worker_image
        working_dir = "/app"
        command     = ["python", "-m", "pbmc_platform.run_dbt_handoff"]
        args = [
          "--project", "pbmc-okf-portfolio",
          "--bucket", google_storage_bucket.pipeline_data.name,
          "--release-id", var.warehouse_release_id,
          "--raw-dataset", data.google_bigquery_dataset.raw.dataset_id,
          "--location", "europe-west1",
        ]
        resources {
          limits = {
            cpu    = "1"
            memory = "2Gi"
          }
        }
      }
    }
  }
  depends_on = [
    google_project_service.run,
    google_artifact_registry_repository.containers,
    google_project_iam_member.dbt_jobs,
    google_bigquery_dataset_iam_member.dbt_raw,
    google_bigquery_dataset_iam_member.dbt_analytics,
    google_storage_bucket_iam_member.dbt_read,
    google_storage_bucket_iam_member.dbt_artifacts,
  ]
}

output "dbt_job_name" {
  value = google_cloud_run_v2_job.dbt.name
}

output "analytics_dataset" {
  value = google_bigquery_dataset.analytics.dataset_id
}
