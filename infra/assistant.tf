variable "assistant_image" {
  type        = string
  description = "Digest-pinned assistant image containing one verified public-data snapshot."
  default     = "europe-west1-docker.pkg.dev/pbmc-okf-portfolio/pbmc-containers/pbmc-assistant@sha256:837337193ef8e03d43c3c475b596f1873af8d0c16ae76ad7583c34ccfd98e223"
  validation {
    condition     = can(regex("^europe-west1-docker[.]pkg[.]dev/pbmc-okf-portfolio/pbmc-containers/pbmc-assistant@sha256:[a-f0-9]{64}$", var.assistant_image))
    error_message = "Assistant image must use an explicit Artifact Registry SHA-256 digest."
  }
}

variable "assistant_invoker_members" {
  type        = set(string)
  description = "Named users/groups/service accounts allowed to invoke this service; no public principals."
  default     = []
  validation {
    condition     = alltrue([for member in var.assistant_invoker_members : can(regex("^(user|group|serviceAccount):[^ @]+@[^ @]+$", member))])
    error_message = "Use named user:, group: or serviceAccount: email principals; public access is not supported."
  }
}

resource "google_service_account" "assistant" {
  project      = "pbmc-okf-portfolio"
  account_id   = "pbmc-assistant"
  display_name = "PBMC evidence assistant snapshot reader"
  description  = "Serves the bundled verified snapshot; no BigQuery or Storage grants."
  depends_on   = [google_project_service.iam]
}

resource "google_cloud_run_v2_service" "assistant" {
  project              = "pbmc-okf-portfolio"
  name                 = "pbmc-assistant"
  location             = "europe-west1"
  deletion_protection  = false
  ingress              = "INGRESS_TRAFFIC_ALL"
  invoker_iam_disabled = false
  labels               = { application = "pbmc-platform", environment = "portfolio" }

  template {
    service_account                  = google_service_account.assistant.email
    timeout                          = "30s"
    max_instance_request_concurrency = 8
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }
    containers {
      image = var.assistant_image
      ports { container_port = 8080 }
      resources {
        limits            = { cpu = "1", memory = "1Gi" }
        cpu_idle          = true
        startup_cpu_boost = true
      }
      startup_probe {
        timeout_seconds   = 5
        period_seconds    = 5
        failure_threshold = 24
        http_get {
          path = "/ready"
          port = 8080
        }
      }
    }
  }
  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }
  depends_on = [google_project_service.run]
}

resource "google_cloud_run_v2_service_iam_member" "assistant_invoker" {
  for_each = var.assistant_invoker_members
  project  = "pbmc-okf-portfolio"
  location = google_cloud_run_v2_service.assistant.location
  name     = google_cloud_run_v2_service.assistant.name
  role     = "roles/run.invoker"
  member   = each.value
}

output "assistant_service_uri" {
  value = google_cloud_run_v2_service.assistant.uri
}

output "assistant_service_account" {
  value = google_service_account.assistant.email
}
