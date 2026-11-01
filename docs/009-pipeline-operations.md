# Failure, retry and recovery runbook

Stage 4, substep 4 checks how the pipeline fails and how an operator recovers.
In simple terms: stop the assembly line when a check fails, keep the evidence,
and reuse work that has already passed its checks when restarting.

## Isolated live drills

`infra/drills.tf` provisions a separate `pbmc-pipeline-drills` workflow, three
`pbmc-drill-*` jobs, four dedicated service accounts, the private bucket
`pbmc-okf-portfolio-drills`, raw dataset `pbmc_drill_raw` and analytics dataset
`pbmc_drill_analytics_r_<release_id>`. Worker grants apply to these resources;
the drill service accounts receive no production data grants. Both BigQuery
workers have project Job User permission, with data access limited to the
drill datasets. The workflow can execute/read only drill jobs and read their
QC receipts. Worker bucket grants allow reading and create-only publication
inside the drill bucket. No drill schedule is configured.

The drill workflow is generated from the production pipeline template. Its
changes supply drill resource names, a scenario environment variable and a
drill analytics base. The production workflow's source and images are unchanged.
`Dockerfile.drills` adds `ops/drill_worker.py` to the pinned existing QC or
warehouse runtime. The injected faults are packaged only in drill images.
The entrypoint rejects production bucket/dataset names before doing work.

| Scenario | Injected condition | Required behavior |
| --- | --- | --- |
| `qc_fail` | QC fails on every attempt | Two QC attempts; workflow fails; no load or dbt start |
| `missing_handoff` | Successful QC omits its receipt | Workflow fails reading receipt; no downstream start |
| `invalid_handoff` | QC receipt names an unprovisioned release | Release gate fails; no downstream start |
| `load_fail` | Loader fails on every attempt | Two loader attempts; workflow fails; dbt does not start |
| `load_retry` | Loader fails after its first table is committed/verified | Second attempt verifies/reuses that table, finishes loading, and dbt succeeds |
| `dbt_fail` | An extra singular test returns one failure row | Both dbt attempts fail; failed results, manifest and log remain available |
| `healthy` | Normal run after the failed dbt build | Existing raw tables are reused without load jobs; all six models and 30 normal tests pass |

From the repository root, with ADC and warehouse requirements installed:

```powershell
$env:PYTHONPATH = 'src'
.venv\Scripts\python.exe ops/run_drills.py
```

Run one suite at a time. The runner refuses to start while an earlier drill
job is active. It copies the existing public raw input and committed release
into the drill bucket with create-only preconditions, preserving the original
production objects. Each case records its workflow name before polling,
checks which jobs started, checks terminal counts and task-attempt logs,
and compares processing audits with their durable GCS copies. Failed dbt
results are checked for the deliberately injected failing test, while all
30 normal tests must pass. The final healthy run must reconcile QC totals.
New BigQuery load jobs are checked for duplicate table loads. Production
object generations and raw/analytics table modification times and row counts
are compared before and after the entire suite.

Evidence is saved incrementally under `artifacts/drills/<UTC timestamp>/report.json`.
Reports are generated locally and excluded from Git. Full worker evidence
remains under the drill bucket's `runs/` prefixes. Drill resources are retained
for repeatable demonstrations; jobs have no idle execution and no scheduler.
Buckets and nonempty datasets cannot be destroyed with their contents by
these Terraform settings. Review a separate cleanup plan if retiring them.

If verification stops, diagnose the error and confirm all earlier jobs have
finished. Resume a failed local report to retain already verified cases:

```powershell
.venv\Scripts\python.exe ops/run_drills.py `
  --resume artifacts/drills/UTC_TIMESTAMP/report.json
```

Resume requires unchanged production snapshots. It keeps the original suite
start time and raw-table baseline, records previous failed verification attempts,
and reruns only cases that have not passed. Logs are queried with an execution
time boundary and all continuation pages, including empty pages with a next token.

## Diagnose a failed production run

Inspect the workflow using its execution UUID:

```powershell
gcloud.cmd workflows executions describe WORKFLOW_EXECUTION_UUID `
  --workflow=pbmc-qc-workflow --project=pbmc-okf-portfolio --location=europe-west1
```

Find the failing step in `error.context` and inspect the relevant Cloud Run
execution. If a failed workflow has no final result, list recent executions
for the job named by the failing step and match their creation times.

```powershell
gcloud.cmd run jobs executions list --job=pbmc-warehouse-load `
  --project=pbmc-okf-portfolio --region=europe-west1 --limit=5
gcloud.cmd run jobs executions describe CLOUD_RUN_EXECUTION_NAME `
  --project=pbmc-okf-portfolio --region=europe-west1
```

Open the execution's `logUri` to see the Python error and structured
`cloud_qc_run`, `cloud_warehouse_run` or `cloud_dbt_run` event. Match execution
name, release ID and run UUID to the durable audit. For dbt, inspect
`runs/dbt/<run_id>/run_results.json`, `manifest.json` and `dbt.log`.
An audit with successful processing does not prove successful artifact delivery;
accept the run only when the job and workflow are terminal successes.

## Recover without losing validated work

| Failure | Recovery |
| --- | --- |
| Missing permission or wrong region | Correct the scoped grant/configuration, then rerun the workflow |
| QC checksum/identity mismatch | Investigate the source or configuration; keep the committed release immutable |
| Unprovisioned release | Add the new analytics dataset and grants while retaining earlier releases, then update the selected release |
| Partial warehouse load | Rerun the same release; the loader verifies completed tables and fills only missing/empty tables |
| Existing raw rows differ from the release | Investigate tampering or an incorrect target; do not truncate validated tables to bypass the check |
| dbt SQL or data-test failure | Read saved results, correct the cause, and rerun; analytics may be partially rebuilt until success |
| Audit/artifact upload failure | Restore prefix access or connectivity and rerun; processing success alone is insufficient |
| Workflow timeout, cancellation or lost start response | Inspect running job executions before restarting; a workflow failure does not itself cancel a job |

After resolving the cause and confirming no earlier execution is still running:

```powershell
gcloud.cmd workflows run pbmc-qc-workflow `
  --project=pbmc-okf-portfolio --location=europe-west1
```

Run one workflow at a time per release because dbt rebuilds its analytics in
place. The pipeline has one Cloud Run task retry, meaning at most two task
attempts. There is no explicit workflow retry around a job-start request.
Rerunning the workflow creates fresh audits and execution receipts. QC reuses
verified immutable files; loading verifies existing rows; dbt rebuilds and
retests its release-scoped analytics. Review the final successful release ID,
all three execution names, six models and 30 passing tests before accepting it.

If Terraform reports a stale state lock, check that the owning Terraform
process has ended and verify the lock ID and owner. Only then release that
specific stale lock. Keep normal locking enabled for plans and applies.

## Verified results — 2026-11-01

All seven live verification cases passed. `FAILED` is the expected workflow
state for deliberately injected failures; a failed workflow alone is never
accepted without checking the intended error, attempts, artifacts and stopped
downstream jobs.

| Scenario | Observed workflow state | Workflow execution UUID |
| --- | --- | --- |
| QC failure | FAILED, two QC attempts | `92f641b8-983a-44dc-978d-4e837c0a3f79` |
| Missing receipt | FAILED before loading | `025a409c-f848-4f07-ab16-2dd2a8a39c3c` |
| Invalid release receipt | FAILED at release gate | `85b948d6-b927-4039-82cb-c9c30e403d9f` |
| Permanent loader failure | FAILED, two loader attempts, no dbt | `de7e9af9-c0a8-44d4-a350-030eed8093a4` |
| Partial-load retry | SUCCEEDED after second loader attempt | `a5027e25-59fa-44a4-83f7-8a7f55b2ab8d` |
| Failing dbt data test | FAILED, both attempts saved failed test artifacts | `c7ba5c90-303d-4555-8ade-6a1186094254` |
| Clean recovery | SUCCEEDED with six models and 30 passing tests | `560272a9-bfe0-4e65-8d00-5883851018f8` |

Both dbt failure attempts failed only the injected test while the 30 normal
tests passed. The complete suite created exactly three BigQuery load jobs,
one each for cells, genes and dataset summary; retries and subsequent runs
did not submit duplicate loads. Recovery reconciled 2,700 cells, 2,638 passes,
62 failures, 32,738 features and 6,390,631 counts. The cohort table had 2,638 rows.
Production object generations and raw/analytics table metadata were unchanged
from the suite's initial snapshot.

The full Python suite passed with 103 tests, including nine drill checks for
resource isolation, partial-table failure injection, empty-page log pagination
and a writable private copy of a read-only dbt project. A non-root Linux smoke
test verified injected-test setup without changing the original project.
Terraform formatting, validation and the final drift check passed with no changes.
The initial deployment added 29 isolated resources, mostly IAM grants, and the
subsequent fix updated only the image references on two drill jobs. Production resources
were not updated.

The pinned drill image index digests are:

- QC `v0.1.0`: `sha256:267c6a16aa74d196e8316c3395b15c2b530e528fe6a17bd778a154559bfd9124`.
- Warehouse/dbt `v0.1.1`: `sha256:19d297bfec194b7a4550084c6a15d5a42a4bdaef861e15afea9fc620c8c18754`.

The completed local evidence report is
`artifacts/drills/20261101T165506Z/report.json`, including earlier failed
verification history retained during resume. Stage 4's four automation
substeps are complete. Next: Stage 5, generating and validating OKF outputs.

## Validation limits

The live suite covers application errors, absent/invalid handoff, exhausted
task retries, recovery from a partial load and recovery from failed dbt tests.
It does not establish timeout/cancellation behavior through live fault
injection or provide a distributed concurrency lock, alert delivery, schedules
or production SLOs. Those remain later operations work. Data is the bounded
public PBMC3k portfolio dataset, with one healthy donor and no clinical inference.

## References

- [Cloud Run task attempt environment variable](https://docs.cloud.google.com/run/docs/container-contract).
- [Cloud Run maximum retries](https://docs.cloud.google.com/run/docs/configuring/max-retries).
- [Workflows retry semantics](https://docs.cloud.google.com/workflows/docs/reference/syntax/retrying).
