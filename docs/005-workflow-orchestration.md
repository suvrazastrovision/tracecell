# Pipeline orchestration with Google Cloud Workflows

`infra/workflows.tf` defines `pbmc-qc-workflow` in `europe-west1`, using
`infra/pipeline.yaml.tftpl`. It runs QC, reads the successful execution's
committed receipt, then runs the BigQuery loader and dbt for that exact release.
Each Cloud Run v1 connector call waits for completion. The region is explicit.
A failed stage stops the workflow. See the [complete release handoff](008-pipeline-handoff.md).

## Identity and execution controls

The dedicated `pbmc-workflow-runner` service account receives job-scoped
`roles/run.invoker` and `roles/run.viewer` grants on QC. The loader and dbt
jobs grant job-scoped `roles/run.jobsExecutorWithOverrides` and `roles/run.viewer`
so the workflow can pass the selected release and monitor completion.
Its only bucket grant reads `runs/handoffs/qc/` objects. It cannot update job
definitions, change their container images, write bucket objects or access
BigQuery directly. Each job retains its separate data-access service account.

Connector timeouts are 2,100 seconds for QC and loading, and 2,700 seconds for
dbt, allowing two possible task attempts plus startup time. There is no explicit
workflow retry policy around starting jobs. Cloud Run handles each configured single task
retry. Workflow timeout does not itself cancel an already running Cloud Run
execution; inspect execution names and platform logs after failures.
No schedule or event trigger is configured.

## Run and inspect

After applying the Terraform plan, run:

```powershell
gcloud.cmd workflows run pbmc-qc-workflow `
  --project=pbmc-okf-portfolio `
  --location=europe-west1
```

Confirm the workflow reaches `SUCCEEDED`, then inspect all three execution names
returned with the release ID, analytics dataset and receipt generation. For a matching published release, QC's structured
`cloud_qc_run` log and GCS audit should show `qc_reused`, `restored_from_cloud`
and publication `reused` as true. The QC summary must reconcile and the object
generations must remain unchanged. Each invocation creates a new run audit.

This workflow orchestrates QC, release publication, [BigQuery loading](006-cloud-warehouse.md),
[dbt analytics](007-cloud-dbt.md) and their run audits. The next automation
substep is broader full-chain failure/retry and operational verification.
OKF generation and the scientist assistant remain subsequent portfolio stages.

## Historical QC-only baseline verification

Verified on 2026-11-01 (Europe/Berlin):

- Workflow execution `e49d8b7b-d033-4933-a4b5-2ce952e5baf2` reached
  `SUCCEEDED` in approximately 60 seconds, using revision `000001-3e9`.
- The returned Cloud Run execution `pbmc-qc-zzd5n` completed successfully.
  Its creator was the dedicated `pbmc-workflow-runner` service account.
- QC reused Linux release
  `71a4bb4d026366b644c4aaf8bd4b29d8f8f87fe1701ba2e8d6a88ba164b83f7d`.
  Source and output SHA-256 checks, release identity, Parquet keys and summary
  reconciliation all passed; the durable audit matched its structured log.
- The workflow-triggered run preserved all published object generations from
  the two directly executed Cloud Run runs. Counts remained 2,700 cells,
  2,638 QC passes and 62 failures.
- The final Terraform plan reported no changes. Local evidence is stored in
  `artifacts/workflow_execution.json`, `artifacts/workflow_verification.json`
  and `artifacts/cloud_run_verification.json`. These generated files are
  excluded from Git; credentials are not included in the reports.

## References

- [Execute Cloud Run jobs using Workflows](https://docs.cloud.google.com/workflows/docs/tutorials/execute-cloud-run-jobs).
- [Cloud Run v1 job-run connector](https://docs.cloud.google.com/workflows/docs/reference/googleapis/run/v1/namespaces.jobs/run).
