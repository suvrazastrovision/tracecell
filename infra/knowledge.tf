variable "knowledge_worker_image" {
  type        = string
  description = "Digest-pinned image containing dbt handoff and knowledge workers."
  default     = "europe-west1-docker.pkg.dev/pbmc-okf-portfolio/pbmc-containers/pbmc-knowledge@sha256:078bb88fff58ab7147b0188e1b1be15ed10c85dceefab1adb738058ece510f95"
  validation {
    condition     = can(regex("^europe-west1-docker[.]pkg[.]dev/pbmc-okf-portfolio/pbmc-containers/[a-z0-9-]+@sha256:[a-f0-9]{64}$", var.knowledge_worker_image))
    error_message = "Knowledge worker image must use an explicit Artifact Registry SHA-256 digest."
  }
}

resource "google_service_account" "knowledge_runner" {
  project      = "pbmc-okf-portfolio"
  account_id   = "pbmc-knowledge-runner"
  display_name = "PBMC verified knowledge publisher"
  description  = "Reads selected release and warehouse rows; creates verified knowledge outputs."
  depends_on   = [google_project_service.iam]
}

resource "google_bigquery_dataset_iam_member" "knowledge_read" {
  for_each   = { raw = data.google_bigquery_dataset.raw.dataset_id, analytics = google_bigquery_dataset.analytics.dataset_id }
  project    = "pbmc-okf-portfolio"
  dataset_id = each.value
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.knowledge_runner.email}"
}

locals {
  knowledge_read_prefixes = ["processed/releases/", "runs/dbt/", "runs/cloud_dbt/", "runs/handoffs/dbt/",
    "knowledge/releases/", "runs/knowledge_publication/", "runs/knowledge_generation/",
  "runs/knowledge_validation/", "runs/cloud_knowledge/", "runs/handoffs/knowledge/"]
  knowledge_write_prefixes = ["knowledge/releases/", "runs/knowledge_publication/", "runs/knowledge_generation/",
  "runs/knowledge_validation/", "runs/cloud_knowledge/", "runs/handoffs/knowledge/"]
}

resource "google_storage_bucket_iam_member" "knowledge_read" {
  bucket = google_storage_bucket.pipeline_data.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.knowledge_runner.email}"
  condition {
    title      = "knowledge-read-selected-inputs-and-own-outputs"
    expression = join(" || ", [for prefix in local.knowledge_read_prefixes : "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.pipeline_data.name}/objects/${prefix}')"])
  }
}

resource "google_storage_bucket_iam_member" "knowledge_create" {
  bucket = google_storage_bucket.pipeline_data.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.knowledge_runner.email}"
  condition {
    title      = "knowledge-create-only-output-prefixes"
    expression = join(" || ", [for prefix in local.knowledge_write_prefixes : "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.pipeline_data.name}/objects/${prefix}')"])
  }
}

resource "google_cloud_run_v2_job" "knowledge" {
  project             = "pbmc-okf-portfolio"
  name                = "pbmc-knowledge-build"
  location            = "europe-west1"
  deletion_protection = false
  labels              = { application = "pbmc-platform", environment = "portfolio" }
  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = google_service_account.knowledge_runner.email
      timeout         = "1800s"
      max_retries     = 1
      containers {
        image       = var.knowledge_worker_image
        working_dir = "/app"
        command     = ["python", "-m", "pbmc_platform.run_cloud_knowledge"]
        args = ["--project", "pbmc-okf-portfolio", "--bucket", google_storage_bucket.pipeline_data.name,
          "--release-id", var.warehouse_release_id, "--dbt-run-id", "REQUIRES_WORKFLOW_OVERRIDE",
          "--dbt-execution", "REQUIRES_WORKFLOW_OVERRIDE", "--dbt-handoff-generation", "0",
        "--raw-dataset", data.google_bigquery_dataset.raw.dataset_id, "--location", "europe-west1"]
        resources {
          limits = { cpu = "1", memory = "2Gi" }
        }
      }
    }
  }
  depends_on = [google_project_service.run, google_storage_bucket_iam_member.knowledge_read,
  google_storage_bucket_iam_member.knowledge_create, google_bigquery_dataset_iam_member.knowledge_read]
}

output "knowledge_job_name" {
  value = google_cloud_run_v2_job.knowledge.name
}
