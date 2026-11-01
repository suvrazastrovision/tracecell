resource "google_project_service" "run" {
  project            = "pbmc-okf-portfolio"
  service            = "run.googleapis.com"
  disable_on_destroy = false
}

resource "google_cloud_run_v2_job" "qc" {
  project             = "pbmc-okf-portfolio"
  name                = "pbmc-qc"
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
      service_account = google_service_account.qc_runner.email
      timeout         = "900s"
      max_retries     = 1

      containers {
        image       = "europe-west1-docker.pkg.dev/pbmc-okf-portfolio/pbmc-containers/pbmc-qc@sha256:f5eea9f5f0e0ac564c0ecedcb9e562288eec5507690828570fc065264302ae93"
        working_dir = "/app"
        command     = ["python", "-m", "pbmc_platform.run_cloud_qc"]
        args = [
          "--project", "pbmc-okf-portfolio",
          "--bucket", google_storage_bucket.pipeline_data.name,
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
    google_storage_bucket_iam_member.qc_read,
    google_storage_bucket_iam_member.qc_create,
  ]
}

output "qc_job_name" {
  value = google_cloud_run_v2_job.qc.name
}
