locals {
  drill_accounts = {
    qc        = google_service_account.drill["qc"].email
    warehouse = google_service_account.drill["warehouse"].email
    dbt       = google_service_account.drill["dbt"].email
  }
  drill_storage_grants = merge(
    { for key, account in local.drill_accounts : "${key}_read" => { account = account, role = "roles/storage.objectViewer" } },
    { for key, account in local.drill_accounts : "${key}_create" => { account = account, role = "roles/storage.objectCreator" } },
  )
  # Preserve production control flow; substitute only isolated resource names.
  drill_base = yamldecode(templatefile("${path.module}/pipeline.yaml.tftpl", {
    project       = "pbmc-okf-portfolio", location = "europe-west1",
    qc_job        = google_cloud_run_v2_job.drill["qc"].name,
    warehouse_job = google_cloud_run_v2_job.drill["warehouse"].name,
    dbt_job       = google_cloud_run_v2_job.drill["dbt"].name,
    bucket        = google_storage_bucket.drill.name, raw_dataset = google_bigquery_dataset.drill["raw"].dataset_id,
    release_id    = var.warehouse_release_id, analytics = google_bigquery_dataset.drill["analytics"].dataset_id,
  }))
  # Fault selection is an execution override on drill jobs, never a production input.
  drill_calls = merge([for step in local.drill_base.main.steps : { for name, config in step : name => config if startswith(name, "run_") }]...)
  drill_overrides = { for name, config in local.drill_calls : name => merge(config, {
    args = merge(config.args, { body = { overrides = { containerOverrides = [merge(
      try(config.args.body.overrides.containerOverrides[0], {}),
      { env = [{ name = "PBMC_DRILL_SCENARIO", value = "$${input.scenario}" }] },
      name == "run_dbt" ? { args = concat(config.args.body.overrides.containerOverrides[0].args, ["--analytics-base", "pbmc_drill_analytics"]) } : {},
    )] } } })
  }) }
  # JSON roundtripping preserves the different shapes of call and control-flow steps.
  drill_steps = [for step in local.drill_base.main.steps : { for name, config in step : name => jsondecode(
    contains(keys(local.drill_overrides), name) ? jsonencode(local.drill_overrides[name]) : jsonencode(config)
  ) }]
}

resource "google_service_account" "drill" {
  for_each     = toset(["qc", "warehouse", "dbt", "workflow"])
  project      = "pbmc-okf-portfolio"
  account_id   = "pbmc-drill-${each.key}"
  display_name = "Isolated PBMC ${each.key} drills"
}

resource "google_project_iam_member" "drill_jobs" {
  for_each = toset(["warehouse", "dbt"])
  project  = "pbmc-okf-portfolio"
  role     = "roles/bigquery.jobUser"
  member   = "serviceAccount:${google_service_account.drill[each.key].email}"
}

resource "google_storage_bucket" "drill" {
  name                        = "pbmc-okf-portfolio-drills"
  location                    = "europe-west1"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
}

resource "google_storage_bucket_iam_member" "drill_workers" {
  for_each = local.drill_storage_grants
  bucket   = google_storage_bucket.drill.name
  role     = each.value.role
  member   = "serviceAccount:${each.value.account}"
}

resource "google_bigquery_dataset" "drill" {
  for_each = {
    raw       = "pbmc_drill_raw"
    analytics = "pbmc_drill_analytics_r_${var.warehouse_release_id}"
  }
  project                    = "pbmc-okf-portfolio"
  dataset_id                 = each.value
  location                   = "europe-west1"
  description                = "Isolated public-data failure/retry drills; not portfolio production outputs."
  delete_contents_on_destroy = false
}

resource "google_bigquery_dataset_iam_member" "drill" {
  for_each = {
    loader = { dataset = "raw", account = "warehouse", role = "roles/bigquery.dataEditor" }
    source = { dataset = "raw", account = "dbt", role = "roles/bigquery.dataViewer" }
    target = { dataset = "analytics", account = "dbt", role = "roles/bigquery.dataEditor" }
  }
  project    = "pbmc-okf-portfolio"
  dataset_id = google_bigquery_dataset.drill[each.value.dataset].dataset_id
  role       = each.value.role
  member     = "serviceAccount:${local.drill_accounts[each.value.account]}"
}

resource "google_cloud_run_v2_job" "drill" {
  for_each            = local.drill_accounts
  project             = "pbmc-okf-portfolio"
  name                = "pbmc-drill-${each.key}"
  location            = "europe-west1"
  deletion_protection = false
  labels              = { application = "pbmc-platform", environment = "drill" }
  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = each.value
      timeout         = each.key == "dbt" ? "1200s" : "900s"
      max_retries     = 1
      containers {
        image       = each.key == "qc" ? "europe-west1-docker.pkg.dev/pbmc-okf-portfolio/pbmc-containers/pbmc-drill-qc@sha256:267c6a16aa74d196e8316c3395b15c2b530e528fe6a17bd778a154559bfd9124" : "europe-west1-docker.pkg.dev/pbmc-okf-portfolio/pbmc-containers/pbmc-drill-warehouse@sha256:19d297bfec194b7a4550084c6a15d5a42a4bdaef861e15afea9fc620c8c18754"
        working_dir = "/app"
        command     = ["python", "/app/ops/drill_worker.py"]
        args        = ["--project", "pbmc-okf-portfolio", "--bucket", google_storage_bucket.drill.name]
        env {
          name  = "PBMC_DRILL_STAGE"
          value = each.key
        }
        resources {
          limits = { cpu = "1", memory = "2Gi" }
        }
      }
    }
  }
  depends_on = [google_storage_bucket_iam_member.drill_workers, google_bigquery_dataset_iam_member.drill, google_project_iam_member.drill_jobs]
}

resource "google_cloud_run_v2_job_iam_member" "drill_execute" {
  for_each = local.drill_accounts
  project  = "pbmc-okf-portfolio"
  location = "europe-west1"
  name     = google_cloud_run_v2_job.drill[each.key].name
  role     = "roles/run.jobsExecutorWithOverrides"
  member   = "serviceAccount:${google_service_account.drill["workflow"].email}"
}

resource "google_cloud_run_v2_job_iam_member" "drill_read" {
  for_each = local.drill_accounts
  project  = "pbmc-okf-portfolio"
  location = "europe-west1"
  name     = google_cloud_run_v2_job.drill[each.key].name
  role     = "roles/run.viewer"
  member   = "serviceAccount:${google_service_account.drill["workflow"].email}"
}

resource "google_storage_bucket_iam_member" "drill_handoff" {
  bucket = google_storage_bucket.drill.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.drill["workflow"].email}"
  condition {
    title      = "drill-workflow-read-handoffs"
    expression = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.drill.name}/objects/runs/handoffs/qc/')"
  }
}

resource "google_workflows_workflow" "drill" {
  project         = "pbmc-okf-portfolio"
  name            = "pbmc-pipeline-drills"
  region          = "europe-west1"
  service_account = google_service_account.drill["workflow"].id
  description     = "Isolated failure and retry tests rendered from the production pipeline template."
  source_contents = yamlencode({ main = { params = ["input"], steps = local.drill_steps } })
  depends_on      = [google_cloud_run_v2_job_iam_member.drill_execute, google_cloud_run_v2_job_iam_member.drill_read, google_storage_bucket_iam_member.drill_handoff]
}
