# QC orchestration with Google Cloud Workflows

`infra/workflows.tf` defines `pbmc-qc-workflow` in `europe-west1`. It starts
the existing `pbmc-qc` Cloud Run Job and waits for completion using the Cloud
Run v1 connector. The region is supplied explicitly to avoid the global
endpoint. The result is the completed Cloud Run execution, including its name,
completion conditions and log URI. A failed job propagates as a failed workflow.

## Identity and execution controls

The dedicated `pbmc-workflow-runner` service account receives job-scoped
`roles/run.invoker` and `roles/run.viewer` grants. The connector can start the
job and monitor its execution. The workflow has no bucket grants or permission
to update the job, choose another container, or supply execution overrides.
The job retains its separate `pbmc-qc-runner` identity for data access.

The connector timeout is 2,100 seconds, allowing the job's two possible
900-second attempts plus startup time. There is no explicit workflow retry
policy around starting the job. Cloud Run handles the configured single task
retry. Workflow timeout does not itself cancel an already running Cloud Run
execution; inspect the returned execution and platform logs after failures.
No schedule or event trigger is configured.

## Run and inspect

After applying the Terraform plan, run:

```powershell
gcloud.cmd workflows run pbmc-qc-workflow `
  --project=pbmc-okf-portfolio `
  --location=europe-west1
```

Confirm the workflow reaches `SUCCEEDED`, then inspect the Cloud Run execution
returned in its result. For a matching published release, the job's structured
`cloud_qc_run` log and GCS audit should show `qc_reused`, `restored_from_cloud`
and publication `reused` as true. The QC summary must reconcile and the object
generations must remain unchanged. Each invocation creates a new run audit.

This workflow currently orchestrates ingestion, QC, release publication and
run audits. The BigQuery loader and dbt build still run separately; adding
those cloud stages is the next automation milestone. OKF generation and the
scientist assistant remain subsequent portfolio stages.

## Live verification

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
