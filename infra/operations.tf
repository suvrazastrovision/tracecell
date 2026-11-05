variable "pipeline_schedule" {
  type        = string
  default     = "0 9 * * 1"
  description = "Pipeline cadence; default Monday 09:00 Europe/Berlin."
}
variable "pipeline_schedule_paused" {
  type    = bool
  default = false
}

resource "google_project_service" "operations" {
  for_each           = toset(["cloudscheduler.googleapis.com", "monitoring.googleapis.com", "pubsub.googleapis.com"])
  project            = "pbmc-okf-portfolio"
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "scheduler" {
  project      = "pbmc-okf-portfolio"
  account_id   = "pbmc-scheduler"
  display_name = "PBMC weekly pipeline trigger"
}

resource "google_service_account" "guard_drill" {
  project      = "pbmc-okf-portfolio"
  account_id   = "pbmc-guard-drill"
  display_name = "PBMC isolated concurrency guard checks"
}

resource "google_project_iam_custom_role" "pipeline_lock" {
  project     = "pbmc-okf-portfolio"
  role_id     = "pbmcPipelineLock"
  title       = "PBMC generation-conditional pipeline lock"
  permissions = ["storage.objects.create", "storage.objects.get", "storage.objects.delete"]
}

locals {
  pipeline_guards = {
    production = { bucket = google_storage_bucket.pipeline_data.name, object = "operations/locks/production-pipeline.json", email = google_service_account.workflow_runner.email }
    drill      = { bucket = google_storage_bucket.drill.name, object = "operations/locks/pipeline-guard-drill.json", email = google_service_account.guard_drill.email }
  }
}

resource "google_storage_bucket_iam_member" "pipeline_lock" {
  for_each = local.pipeline_guards
  bucket   = each.value.bucket
  role     = google_project_iam_custom_role.pipeline_lock.id
  member   = "serviceAccount:${each.value.email}"
  condition {
    title      = "exact-pipeline-lock-only"
    expression = "resource.name == 'projects/_/buckets/${each.value.bucket}/objects/${each.value.object}'"
  }
}

resource "google_project_iam_member" "guard_logging" {
  for_each = local.pipeline_guards
  project  = "pbmc-okf-portfolio"
  role     = "roles/logging.logWriter"
  member   = "serviceAccount:${each.value.email}"
}

resource "google_workflows_workflow" "guard_drill" {
  project         = "pbmc-okf-portfolio"
  name            = "pbmc-pipeline-guard-drill"
  region          = "europe-west1"
  description     = "Isolated atomic lock checks; no Cloud Run or data job permissions."
  service_account = google_service_account.guard_drill.id
  source_contents = yamlencode(merge(yamldecode(templatefile("${path.module}/pipeline_guard.yaml.tftpl", {
    bucket = google_storage_bucket.drill.name, lock_object = "operations/locks/pipeline-guard-drill.json", workflow = "pbmc-pipeline-guard-drill"
    })), {
    pipeline = {
      params = ["input"]
      steps = [
        { inject = { switch = [{ condition = "$${map.get(input, \"mode\") == \"fail\"}", steps = [{ fault = { raise = "DRILL: guarded pipeline failure" } }] }] } },
        { hold = { call = "sys.sleep", args = { seconds = 30 } } },
        { complete = { return = { status = "success", isolated = true } } },
      ]
    }
  }))
  depends_on = [google_storage_bucket_iam_member.pipeline_lock, google_project_iam_member.guard_logging]
}

resource "google_project_iam_custom_role" "schedule_trigger" {
  project     = "pbmc-okf-portfolio"
  role_id     = "pbmcScheduleTrigger"
  title       = "PBMC workflow execution creation only"
  permissions = ["workflows.executions.create"]
}

resource "google_project_iam_member" "scheduler" {
  project = "pbmc-okf-portfolio"
  role    = google_project_iam_custom_role.schedule_trigger.id
  member  = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "pipeline" {
  project          = "pbmc-okf-portfolio"
  region           = "europe-west1"
  name             = "pbmc-weekly-pipeline"
  schedule         = var.pipeline_schedule
  time_zone        = "Europe/Berlin"
  paused           = var.pipeline_schedule_paused
  attempt_deadline = "60s"
  retry_config {
    retry_count          = 0
    min_backoff_duration = "5s"
    max_backoff_duration = "30s"
    max_doublings        = 1
  }
  http_target {
    uri         = "https://workflowexecutions.googleapis.com/v1/${google_workflows_workflow.qc.id}/executions"
    http_method = "POST"
    headers     = { "Content-Type" = "application/json" }
    body        = base64encode(jsonencode({ argument = jsonencode({ trigger = "cloud_scheduler" }), callLogLevel = "LOG_ERRORS_ONLY" }))
    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
  depends_on = [google_project_service.operations, google_project_iam_member.scheduler]
}

resource "google_pubsub_topic" "operations_alerts" {
  project    = "pbmc-okf-portfolio"
  name       = "pbmc-operations-alerts"
  depends_on = [google_project_service.operations]
}

resource "google_pubsub_subscription" "operations_alerts" {
  project                    = "pbmc-okf-portfolio"
  name                       = "pbmc-operations-alert-inbox"
  topic                      = google_pubsub_topic.operations_alerts.id
  message_retention_duration = "604800s"
  expiration_policy { ttl = "" }
}

resource "google_monitoring_notification_channel" "operations" {
  project      = "pbmc-okf-portfolio"
  display_name = "PBMC private operations inbox"
  type         = "pubsub"
  labels       = { topic = google_pubsub_topic.operations_alerts.id }
  depends_on   = [google_project_service.operations]
}

resource "google_pubsub_topic_iam_member" "monitoring" {
  project    = "pbmc-okf-portfolio"
  topic      = google_pubsub_topic.operations_alerts.name
  role       = "roles/pubsub.publisher"
  member     = "serviceAccount:service-288934902878@gcp-sa-monitoring-notification.iam.gserviceaccount.com"
  depends_on = [google_monitoring_notification_channel.operations]
}

resource "google_monitoring_alert_policy" "pipeline_failure" {
  project      = "pbmc-okf-portfolio"
  display_name = "PBMC pipeline failure"
  combiner     = "OR"
  conditions {
    display_name = "Guarded workflow failure"
    condition_matched_log {
      label_extractors = { execution = "EXTRACT(jsonPayload.execution)" }
      filter           = "resource.type=\"workflows.googleapis.com/Workflow\" AND (resource.labels.workflow_id=\"pbmc-qc-workflow\" OR resource.labels.workflow_id=\"pbmc-pipeline-guard-drill\") AND severity>=ERROR"
    }
  }
  notification_channels = [google_monitoring_notification_channel.operations.name]
  alert_strategy {
    notification_rate_limit { period = "300s" }
    auto_close = "1800s"
  }
  documentation {
    mime_type = "text/markdown"
    content   = "Pipeline failed; its generation-bound lock is retained. Inspect the workflow and all Cloud Run executions before releasing it. See docs/021-operations.md. Isolated guard drill failures intentionally exercise this same delivery path."
  }
  depends_on = [google_pubsub_topic_iam_member.monitoring]
}

resource "google_bigquery_dataset" "billing_export" {
  project                    = "pbmc-okf-portfolio"
  dataset_id                 = "pbmc_billing_export"
  friendly_name              = "PBMC actual billing export"
  location                   = "EU"
  delete_contents_on_destroy = false
  lifecycle { prevent_destroy = true }
}

output "operations_alert_subscription" { value = google_pubsub_subscription.operations_alerts.id }
output "pipeline_scheduler_job" { value = google_cloud_scheduler_job.pipeline.id }
output "billing_export_dataset" { value = google_bigquery_dataset.billing_export.id }

# Paused, intentionally missing destination: manual acceptance tests only.
resource "google_cloud_scheduler_job" "failure_drill" {
  project          = "pbmc-okf-portfolio"
  region           = "europe-west1"
  name             = "pbmc-scheduler-failure-drill"
  schedule         = "0 9 * * 1"
  time_zone        = "Europe/Berlin"
  paused           = true
  attempt_deadline = "60s"
  retry_config {
    retry_count          = 0
    min_backoff_duration = "5s"
    max_backoff_duration = "30s"
    max_doublings        = 1
  }
  http_target {
    uri         = "https://workflowexecutions.googleapis.com/v1/projects/pbmc-okf-portfolio/locations/europe-west1/workflows/pbmc-scheduler-missing-target-drill/executions"
    http_method = "POST"
    headers     = { "Content-Type" = "application/json" }
    body        = base64encode(jsonencode({ argument = "{}" }))
    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
  depends_on = [google_project_service.operations, google_project_iam_member.scheduler]
}

resource "google_monitoring_alert_policy" "scheduler_failure" {
  project      = "pbmc-okf-portfolio"
  display_name = "PBMC Scheduler delivery failure"
  combiner     = "OR"
  conditions {
    display_name = "Scheduler error log"
    condition_matched_log {
      filter = "resource.type=\"cloud_scheduler_job\" AND (resource.labels.job_id=\"pbmc-weekly-pipeline\" OR resource.labels.job_id=\"pbmc-scheduler-failure-drill\") AND severity>=ERROR"
    }
  }
  notification_channels = [google_monitoring_notification_channel.operations.name]
  alert_strategy {
    notification_rate_limit { period = "300s" }
    auto_close = "1800s"
  }
  documentation {
    mime_type = "text/markdown"
    content   = "Scheduler could not deliver the workflow start request. Inspect OAuth identity, API availability and the target URI. The paused pbmc-scheduler-failure-drill deliberately targets a nonexistent workflow for manual delivery tests."
  }
  depends_on = [google_pubsub_topic_iam_member.monitoring]
}
