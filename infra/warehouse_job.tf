resource "google_cloud_run_v2_job" "warehouse" {
  project             = "pbmc-okf-portfolio"
  name                = "pbmc-warehouse-load"
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
      service_account = google_service_account.warehouse_runner.email
      timeout         = "900s"
      max_retries     = 1

      containers {
        image       = "europe-west1-docker.pkg.dev/pbmc-okf-portfolio/pbmc-containers/pbmc-warehouse@sha256:bf9dfddaf93d01997fbcf127363193759856545310acafe40ab0e0ef891806bb"
        working_dir = "/app"
        command     = ["python", "-m", "pbmc_platform.run_cloud_warehouse"]
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

  lifecycle {
    precondition {
      condition     = lower(data.google_bigquery_dataset.raw.location) == "europe-west1"
      error_message = "The existing pbmc_raw dataset must be in europe-west1."
    }
  }

  depends_on = [
    google_project_service.run,
    google_artifact_registry_repository.containers,
    google_project_iam_member.warehouse_jobs,
    google_bigquery_dataset_iam_member.warehouse_raw,
    google_storage_bucket_iam_member.warehouse_read,
    google_storage_bucket_iam_member.warehouse_audits,
  ]
}

output "warehouse_job_name" {
  value = google_cloud_run_v2_job.warehouse.name
}

output "warehouse_release_id" {
  value = var.warehouse_release_id
}
