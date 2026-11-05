resource "google_project_service" "workflows" {
  project            = "pbmc-okf-portfolio"
  service            = "workflows.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "workflow_executions" {
  project            = "pbmc-okf-portfolio"
  service            = "workflowexecutions.googleapis.com"
  disable_on_destroy = false
}

resource "google_service_account" "workflow_runner" {
  project      = "pbmc-okf-portfolio"
  account_id   = "pbmc-workflow-runner"
  display_name = "PBMC pipeline workflow"
  description  = "Runs QC, warehouse, dbt and verified knowledge publication using execution receipts."

  depends_on = [google_project_service.iam]
}

resource "google_cloud_run_v2_job_iam_member" "workflow_invoke" {
  project  = google_cloud_run_v2_job.qc.project
  location = google_cloud_run_v2_job.qc.location
  name     = google_cloud_run_v2_job.qc.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.workflow_runner.email}"
}

# The v1 connector polls the execution after starting it. Reading the job and
# its executions is scoped to this job.
resource "google_cloud_run_v2_job_iam_member" "workflow_read" {
  project  = google_cloud_run_v2_job.qc.project
  location = google_cloud_run_v2_job.qc.location
  name     = google_cloud_run_v2_job.qc.name
  role     = "roles/run.viewer"
  member   = "serviceAccount:${google_service_account.workflow_runner.email}"
}

resource "google_workflows_workflow" "qc" {
  project             = "pbmc-okf-portfolio"
  name                = "pbmc-qc-workflow"
  region              = "europe-west1"
  description         = "QC to warehouse to dbt to independently verified knowledge publication."
  service_account     = google_service_account.workflow_runner.id
  deletion_protection = false

  labels = {
    application = "pbmc-platform"
    environment = "portfolio"
  }

  source_contents = yamlencode(merge(
    yamldecode(templatefile("${path.module}/pipeline_guard.yaml.tftpl", {
      bucket   = google_storage_bucket.pipeline_data.name, lock_object = "operations/locks/production-pipeline.json",
      workflow = "pbmc-qc-workflow"
    })),
    { pipeline = merge(yamldecode(templatefile("${path.module}/pipeline.yaml.tftpl", {
      project          = google_cloud_run_v2_job.qc.project
      location         = google_cloud_run_v2_job.qc.location
      qc_job           = google_cloud_run_v2_job.qc.name
      warehouse_job    = google_cloud_run_v2_job.warehouse.name
      dbt_job          = google_cloud_run_v2_job.dbt.name
      bucket           = google_storage_bucket.pipeline_data.name
      raw_dataset      = data.google_bigquery_dataset.raw.dataset_id
      release_id       = var.warehouse_release_id
      analytics        = google_bigquery_dataset.analytics.dataset_id
      enable_knowledge = true
      knowledge_job    = google_cloud_run_v2_job.knowledge.name
  })).main, { params = ["input"] }) }))

  depends_on = [
    google_project_service.workflows,
    google_project_service.workflow_executions,
    google_cloud_run_v2_job_iam_member.workflow_invoke,
    google_cloud_run_v2_job_iam_member.workflow_read,
    google_cloud_run_v2_job_iam_member.workflow_downstream_execute,
    google_cloud_run_v2_job_iam_member.workflow_downstream_read,
    google_storage_bucket_iam_member.workflow_handoff,
    google_storage_bucket_iam_member.pipeline_lock["production"],
    google_project_iam_member.guard_logging["production"],
  ]
}

resource "google_cloud_run_v2_job_iam_member" "workflow_downstream_execute" {
  for_each = {
    warehouse = google_cloud_run_v2_job.warehouse.name
    dbt       = google_cloud_run_v2_job.dbt.name
    knowledge = google_cloud_run_v2_job.knowledge.name
  }
  project  = "pbmc-okf-portfolio"
  location = "europe-west1"
  name     = each.value
  role     = "roles/run.jobsExecutorWithOverrides"
  member   = "serviceAccount:${google_service_account.workflow_runner.email}"
}

resource "google_cloud_run_v2_job_iam_member" "workflow_downstream_read" {
  for_each = {
    warehouse = google_cloud_run_v2_job.warehouse.name
    dbt       = google_cloud_run_v2_job.dbt.name
    knowledge = google_cloud_run_v2_job.knowledge.name
  }
  project  = "pbmc-okf-portfolio"
  location = "europe-west1"
  name     = each.value
  role     = "roles/run.viewer"
  member   = "serviceAccount:${google_service_account.workflow_runner.email}"
}

resource "google_storage_bucket_iam_member" "workflow_handoff" {
  bucket = google_storage_bucket.pipeline_data.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.workflow_runner.email}"
  condition {
    title = "workflow-read-qc-execution-receipts"
    expression = join(" || ", [
      "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.pipeline_data.name}/objects/runs/handoffs/qc/')",
      "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.pipeline_data.name}/objects/runs/handoffs/dbt/')",
      "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.pipeline_data.name}/objects/runs/handoffs/knowledge/')",
    ])
  }
}

output "qc_workflow_name" {
  value = google_workflows_workflow.qc.name
}
