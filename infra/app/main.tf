terraform {
  required_version = ">= 1.5, < 2.0"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 8.0" }
  }
  backend "gcs" {
    bucket = "pbmc-okf-portfolio-tfstate"
    prefix = "terraform/app-pilot"
  }
}
provider "google" {
  project = "pbmc-okf-portfolio"
  region  = "europe-west1"
}
variable "image" {
  type = string
  validation {
    condition     = can(regex("^europe-west1-docker[.]pkg[.]dev/pbmc-okf-portfolio/pbmc-containers/scrna-app@sha256:[a-f0-9]{64}$", var.image))
    error_message = "Use a digest-pinned scrna-app image."
  }
}
variable "invited_emails" {
  type = set(string)
  validation {
    condition     = length(var.invited_emails) > 0 && length(var.invited_emails) <= 5
    error_message = "Invite one to five Google accounts."
  }
}
variable "firebase_config" {
  type    = string
  default = "{}"
}
variable "publish_login" {
  type        = bool
  default     = false
  description = "Enable the sign-in page only after the Google provider and app authentication are verified."
}
variable "app_origin" {
  type    = string
  default = "https://scrna-app-288934902878.europe-west1.run.app"
}
locals {
  project    = "pbmc-okf-portfolio"
  workers    = toset(["inspect", "qc", "load", "dbt", "knowledge"])
  identities = setunion(local.workers, toset(["api", "workflow", "scheduler"]))
  environment = {
    SCRNA_MODE            = "cloud"
    GOOGLE_CLOUD_PROJECT  = local.project
    SCRNA_BUCKET          = google_storage_bucket.app.name
    SCRNA_WORKFLOW        = "scrna-app-pipeline"
    SCRNA_ORIGIN          = var.app_origin
    SCRNA_ALLOWED_EMAILS  = join(",", sort(tolist(var.invited_emails)))
    SCRNA_FIREBASE_CONFIG = var.firebase_config
    SCRNA_DISPATCH_EMAIL  = google_service_account.app["scheduler"].email
  }
}
resource "google_project_service" "api" {
  for_each           = toset(["firestore.googleapis.com", "identitytoolkit.googleapis.com", "firebase.googleapis.com", "run.googleapis.com", "workflows.googleapis.com", "cloudscheduler.googleapis.com"])
  project            = local.project
  service            = each.value
  disable_on_destroy = false
}
resource "google_service_account" "app" {
  for_each     = local.identities
  project      = local.project
  account_id   = "scrna-app-${each.key}"
  display_name = "Single-cell app ${each.key}"
}
resource "google_firestore_database" "app" {
  project                 = local.project
  name                    = "scrna-app"
  location_id             = "europe-west1"
  type                    = "FIRESTORE_NATIVE"
  delete_protection_state = "DELETE_PROTECTION_ENABLED"
  depends_on              = [google_project_service.api]
}
resource "google_storage_bucket" "app" {
  project                     = local.project
  name                        = "pbmc-okf-portfolio-scrna-app"
  location                    = "europe-west1"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  cors {
    origin          = [var.app_origin]
    method          = ["PUT", "POST", "OPTIONS"]
    response_header = ["Content-Type", "Content-Range", "Range"]
    max_age_seconds = 600
  }
  lifecycle_rule {
    condition { age = 35 }
    action { type = "Delete" }
  }
  lifecycle { prevent_destroy = true }
}
resource "google_bigquery_dataset" "app" {
  for_each                    = toset(["raw", "analytics"])
  project                     = local.project
  dataset_id                  = "scrna_app_${each.key}"
  location                    = "europe-west1"
  delete_contents_on_destroy  = false
  default_table_expiration_ms = 2592000000
  lifecycle { prevent_destroy = true }
}
resource "google_project_iam_member" "records" {
  for_each = setunion(local.workers, toset(["api"]))
  project  = local.project
  role     = "roles/datastore.user"
  member   = "serviceAccount:${google_service_account.app[each.key].email}"
  condition {
    title      = "app-database-only"
    expression = "resource.name == 'projects/${local.project}/databases/scrna-app' || resource.name.startsWith('projects/${local.project}/databases/scrna-app/')"
  }
}
resource "google_storage_bucket_iam_member" "objects" {
  for_each = setunion(local.workers, toset(["api"]))
  bucket   = google_storage_bucket.app.name
  role     = "roles/storage.objectUser"
  member   = "serviceAccount:${google_service_account.app[each.key].email}"
}
resource "google_project_iam_member" "bq_jobs" {
  for_each = toset(["load", "dbt", "knowledge"])
  project  = local.project
  role     = "roles/bigquery.jobUser"
  member   = "serviceAccount:${google_service_account.app[each.key].email}"
}
resource "google_bigquery_dataset_iam_member" "raw" {
  for_each   = { load = "roles/bigquery.dataEditor", dbt = "roles/bigquery.dataViewer", knowledge = "roles/bigquery.dataViewer", api = "roles/bigquery.dataEditor" }
  project    = local.project
  dataset_id = google_bigquery_dataset.app["raw"].dataset_id
  role       = each.value
  member     = "serviceAccount:${google_service_account.app[each.key].email}"
}
resource "google_bigquery_dataset_iam_member" "analytics" {
  for_each   = { dbt = "roles/bigquery.dataEditor", knowledge = "roles/bigquery.dataViewer", api = "roles/bigquery.dataEditor" }
  project    = local.project
  dataset_id = google_bigquery_dataset.app["analytics"].dataset_id
  role       = each.value
  member     = "serviceAccount:${google_service_account.app[each.key].email}"
}
resource "google_cloud_run_v2_job" "worker" {
  for_each            = local.workers
  project             = local.project
  name                = "scrna-app-${each.key}"
  location            = "europe-west1"
  deletion_protection = false
  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = google_service_account.app[each.key].email
      timeout         = "1800s"
      max_retries     = 0
      containers {
        image   = var.image
        command = ["python", "-m", "pbmc_platform.app.worker"]
        dynamic "env" {
          for_each = local.environment
          content {
            name  = env.key
            value = env.value
          }
        }
        resources { limits = { cpu = "2", memory = "8Gi" } }
      }
    }
  }
  depends_on = [google_project_service.api, google_storage_bucket_iam_member.objects, google_project_iam_member.records]
}
resource "google_cloud_run_v2_service" "app" {
  project             = local.project
  name                = "scrna-app"
  location            = "europe-west1"
  deletion_protection = false
  ingress             = "INGRESS_TRAFFIC_ALL"
  custom_audiences    = [var.app_origin]
  template {
    service_account                  = google_service_account.app["api"].email
    timeout                          = "300s"
    max_instance_request_concurrency = 8
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
    containers {
      image = var.image
      ports { container_port = 8080 }
      dynamic "env" {
        for_each = local.environment
        content {
          name  = env.key
          value = env.value
        }
      }
      resources {
        limits   = { cpu = "1", memory = "1Gi" }
        cpu_idle = true
      }
      startup_probe {
        http_get {
          path = "/ready"
          port = 8080
          http_headers {
            name  = "Host"
            value = replace(var.app_origin, "https://", "")
          }
        }
      }
    }
  }
  depends_on = [google_project_service.api, google_storage_bucket_iam_member.objects, google_project_iam_member.records]
}
resource "google_project_iam_custom_role" "workflow" {
  project     = local.project
  role_id     = "scrnaAppWorkflowJobs"
  title       = "Single-cell app job execution"
  permissions = ["run.jobs.run", "run.jobs.runWithOverrides", "run.executions.get", "run.executions.list", "run.operations.get"]
}
resource "google_cloud_run_v2_job_iam_member" "workflow" {
  for_each = local.workers
  project  = local.project
  location = "europe-west1"
  name     = google_cloud_run_v2_job.worker[each.key].name
  role     = google_project_iam_custom_role.workflow.name
  member   = "serviceAccount:${google_service_account.app["workflow"].email}"
}
resource "google_workflows_workflow" "app" {
  project             = local.project
  name                = "scrna-app-pipeline"
  region              = "europe-west1"
  service_account     = google_service_account.app["workflow"].id
  deletion_protection = false
  source_contents     = templatefile("${path.module}/pipeline.yaml.tftpl", { project = local.project })
  depends_on          = [google_cloud_run_v2_job_iam_member.workflow]
}
resource "google_project_iam_member" "start" {
  project = local.project
  role    = "roles/workflows.invoker"
  member  = "serviceAccount:${google_service_account.app["api"].email}"
}
resource "google_cloud_run_v2_service_iam_member" "scheduler" {
  project  = local.project
  location = "europe-west1"
  name     = google_cloud_run_v2_service.app.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.app["scheduler"].email}"
}
resource "google_cloud_run_v2_service_iam_member" "public_login" {
  count    = var.publish_login ? 1 : 0
  project  = local.project
  location = "europe-west1"
  name     = google_cloud_run_v2_service.app.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
resource "google_cloud_scheduler_job" "app" {
  for_each  = { dispatch = "* * * * *", cleanup = "0 3 * * *" }
  project   = local.project
  region    = "europe-west1"
  name      = "scrna-app-${each.key}"
  schedule  = each.value
  time_zone = "Europe/Berlin"
  paused    = true
  http_target {
    uri         = "${var.app_origin}/internal/${each.key}"
    http_method = "POST"
    oidc_token {
      service_account_email = google_service_account.app["scheduler"].email
      audience              = var.app_origin
    }
  }
  depends_on = [google_project_service.api]
}
output "app_uri" { value = google_cloud_run_v2_service.app.uri }
output "bucket" { value = google_storage_bucket.app.name }

resource "google_project_iam_member" "workflow_logs" {
  project = local.project
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.app["workflow"].email}"
}
resource "google_monitoring_alert_policy" "app_failure" {
  project               = local.project
  display_name          = "Single-cell app pipeline or dispatcher failure"
  combiner              = "OR"
  notification_channels = ["projects/pbmc-okf-portfolio/notificationChannels/4441981490383563016"]
  conditions {
    display_name = "App workflow and dispatcher errors"
    condition_matched_log {
      filter = "severity>=ERROR AND ((resource.type=\"workflows.googleapis.com/Workflow\" AND resource.labels.workflow_id=\"scrna-app-pipeline\") OR (resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"scrna-app\") OR (resource.type=\"cloud_scheduler_job\" AND (resource.labels.job_id=\"scrna-app-dispatch\" OR resource.labels.job_id=\"scrna-app-cleanup\")))"
    }
  }
  alert_strategy {
    notification_rate_limit { period = "300s" }
    auto_close = "1800s"
  }
}

resource "google_project_iam_custom_role" "sessions" {
  project     = local.project
  role_id     = "scrnaAppSessions"
  title       = "Single-cell app session verification"
  permissions = ["firebaseauth.users.createSession", "firebaseauth.users.get"]
}
resource "google_project_iam_member" "sessions" {
  project = local.project
  role    = google_project_iam_custom_role.sessions.name
  member  = "serviceAccount:${google_service_account.app["api"].email}"
}

resource "google_project_iam_custom_role" "reconcile" {
  project     = local.project
  role_id     = "scrnaAppExecutionRead"
  title       = "Single-cell app owning execution status"
  permissions = ["workflows.executions.get"]
}
resource "google_project_iam_member" "reconcile" {
  project = local.project
  role    = google_project_iam_custom_role.reconcile.name
  member  = "serviceAccount:${google_service_account.app["api"].email}"
}
