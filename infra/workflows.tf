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
  display_name = "PBMC QC workflow"
  description  = "Starts and monitors the PBMC QC Cloud Run job."

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
# its executions is scoped to this job; the workflow has no bucket grants.
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
  description         = "Execute PBMC QC and wait for verified release publication."
  service_account     = google_service_account.workflow_runner.id
  deletion_protection = false

  labels = {
    application = "pbmc-platform"
    environment = "portfolio"
  }

  source_contents = <<-EOT
    main:
      steps:
        - run_qc:
            call: googleapis.run.v1.namespaces.jobs.run
            args:
              name: namespaces/pbmc-okf-portfolio/jobs/${google_cloud_run_v2_job.qc.name}
              location: ${google_cloud_run_v2_job.qc.location}
              body: {}
              connector_params:
                timeout: 2100
            result: qc_execution
        - completed:
            return: $${qc_execution}
  EOT

  depends_on = [
    google_project_service.workflows,
    google_project_service.workflow_executions,
    google_cloud_run_v2_job_iam_member.workflow_invoke,
    google_cloud_run_v2_job_iam_member.workflow_read,
  ]
}

output "qc_workflow_name" {
  value = google_workflows_workflow.qc.name
}
