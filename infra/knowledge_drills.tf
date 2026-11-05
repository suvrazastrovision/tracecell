variable "knowledge_drill_image" {
  type        = string
  description = "Pinned isolated knowledge fault-injection image; never used by production jobs."
  default     = "europe-west1-docker.pkg.dev/pbmc-okf-portfolio/pbmc-containers/pbmc-knowledge-drills@sha256:02888f466cb812acd7cc6da35883f972b6b66a526983cc4a4b2f4d8d3f72bdb4"
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.knowledge_drill_image))
    error_message = "The drill image must use an explicit digest."
  }
}

resource "google_service_account" "knowledge_drill" {
  project      = "pbmc-okf-portfolio"
  account_id   = "pbmc-drill-knowledge"
  display_name = "Isolated knowledge failure and recovery checks"
}

resource "google_bigquery_dataset_iam_member" "knowledge_drill" {
  for_each   = google_bigquery_dataset.drill
  project    = "pbmc-okf-portfolio"
  dataset_id = each.value.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.knowledge_drill.email}"
}

resource "google_storage_bucket_iam_member" "knowledge_drill" {
  for_each = toset(["roles/storage.objectViewer", "roles/storage.objectCreator"])
  bucket   = google_storage_bucket.drill.name
  role     = each.value
  member   = "serviceAccount:${google_service_account.knowledge_drill.email}"
}

resource "google_cloud_run_v2_job" "knowledge_drill_dbt" {
  project             = "pbmc-okf-portfolio"
  name                = "pbmc-drill-dbt-handoff"
  location            = "europe-west1"
  deletion_protection = false
  labels              = { application = "pbmc-platform", environment = "drill" }
  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = google_service_account.drill["dbt"].email
      timeout         = "1200s"
      max_retries     = 1
      containers {
        image       = var.knowledge_worker_image
        working_dir = "/app"
        command     = ["python", "-m", "pbmc_platform.run_dbt_handoff"]
        args = ["--project", "pbmc-okf-portfolio", "--bucket", google_storage_bucket.drill.name,
          "--release-id", var.warehouse_release_id, "--raw-dataset", google_bigquery_dataset.drill["raw"].dataset_id,
        "--analytics-base", "pbmc_drill_analytics", "--location", "europe-west1"]
        resources { limits = { cpu = "1", memory = "2Gi" } }
      }
    }
  }
}

resource "google_cloud_run_v2_job" "knowledge_drill" {
  project             = "pbmc-okf-portfolio"
  name                = "pbmc-drill-knowledge"
  location            = "europe-west1"
  deletion_protection = false
  labels              = { application = "pbmc-platform", environment = "drill" }
  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = google_service_account.knowledge_drill.email
      timeout         = "1800s"
      max_retries     = 1
      containers {
        image       = var.knowledge_drill_image
        working_dir = "/app"
        command     = ["python", "/app/ops/knowledge_drill_worker.py"]
        args        = ["--project", "pbmc-okf-portfolio", "--bucket", google_storage_bucket.drill.name]
        resources { limits = { cpu = "1", memory = "2Gi" } }
      }
    }
  }
  depends_on = [google_storage_bucket_iam_member.knowledge_drill, google_bigquery_dataset_iam_member.knowledge_drill]
}

resource "google_cloud_run_v2_job_iam_member" "knowledge_drill_workflow" {
  for_each = toset(["roles/run.jobsExecutorWithOverrides", "roles/run.viewer"])
  project  = "pbmc-okf-portfolio"
  location = "europe-west1"
  name     = google_cloud_run_v2_job.knowledge_drill.name
  role     = each.value
  member   = "serviceAccount:${google_service_account.drill["workflow"].email}"
}

resource "google_storage_bucket_iam_member" "knowledge_drill_receipts" {
  bucket = google_storage_bucket.drill.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.drill["workflow"].email}"
  condition {
    title = "knowledge-drill-workflow-read-execution-receipts"
    expression = join(" || ", [
      "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.drill.name}/objects/runs/handoffs/dbt/')",
      "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.drill.name}/objects/runs/handoffs/knowledge/')",
    ])
  }
}

locals {
  knowledge_drill_base = yamldecode(templatefile("${path.module}/pipeline.yaml.tftpl", {
    project    = "pbmc-okf-portfolio", location = "europe-west1",
    qc_job     = google_cloud_run_v2_job.drill["qc"].name, warehouse_job = google_cloud_run_v2_job.drill["warehouse"].name,
    dbt_job    = google_cloud_run_v2_job.knowledge_drill_dbt.name, knowledge_job = google_cloud_run_v2_job.knowledge_drill.name,
    bucket     = google_storage_bucket.drill.name, raw_dataset = google_bigquery_dataset.drill["raw"].dataset_id,
    analytics  = google_bigquery_dataset.drill["analytics"].dataset_id,
    release_id = var.warehouse_release_id, enable_knowledge = true,
  }))
  knowledge_drill_names = [for step in local.knowledge_drill_base.main.steps : keys(step)[0]]
  # Reuse the production knowledge boundary, with a prebuilt isolated dbt receipt.
  # Upstream QC/warehouse/dbt correctness already has separate live evidence.
  knowledge_drill_selected = slice(local.knowledge_drill_base.main.steps,
  index(local.knowledge_drill_names, "dbt_receipt_name"), index(local.knowledge_drill_names, "completed"))
  knowledge_drill_steps = [for step in local.knowledge_drill_selected : {
    for name, config in step : name => jsondecode(name == "run_knowledge" ? jsonencode(merge(config, {
      args = merge(config.args, { body = { overrides = { containerOverrides = [{
        args = concat(config.args.body.overrides.containerOverrides[0].args, ["--analytics-base", "pbmc_drill_analytics"]),
        env  = [{ name = "PBMC_KNOWLEDGE_DRILL_SCENARIO", value = "$${input.scenario}" }],
      }] } } })
    })) : jsonencode(config))
  }]
}

resource "google_workflows_workflow" "knowledge_drill" {
  project         = "pbmc-okf-portfolio"
  name            = "pbmc-knowledge-drills"
  region          = "europe-west1"
  service_account = google_service_account.drill["workflow"].id
  description     = "Isolated live knowledge failures, task retries and completion recovery."
  source_contents = yamlencode({ main = { params = ["input"], steps = concat([
    { drill_inputs = { assign = [
      { release_id = var.warehouse_release_id },
      { dbt_execution = { metadata = { name = "$${input.dbt_execution}" } } },
    ] } }
    ], local.knowledge_drill_steps, [
    { completed = { return = {
      status                       = "success", release_id = "$${release_id}",
      dbt_execution                = "$${dbt_execution.metadata.name}", dbt_run_id = "$${dbt_receipt.dbt_run_id}",
      dbt_handoff_generation       = "$${dbt_receipt_metadata.generation}",
      knowledge_execution          = "$${knowledge_execution.metadata.name}",
      knowledge_package_id         = "$${knowledge_receipt.result.package_id}",
      knowledge_publication_uri    = "$${knowledge_receipt.result.publication_uri}",
      knowledge_handoff_generation = "$${knowledge_receipt_metadata.generation}",
    } } }
  ]) } })
  depends_on = [google_cloud_run_v2_job_iam_member.knowledge_drill_workflow, google_storage_bucket_iam_member.knowledge_drill_receipts]
}
