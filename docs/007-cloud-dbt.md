# Building and testing release-scoped analytics on Cloud Run

The dbt worker turns the verified raw tables into six analytics models and
runs all 30 data tests. In simple terms: QC labels the cells, the warehouse
loader files those results, and dbt builds useful summaries and checks that
the numbers agree. This is the second remaining substep of Stage 4 automation.

## Contract and execution

`src/pbmc_platform/run_cloud_dbt.py` requires a full SHA-256 release ID.
It downloads the committed GCS release with generation preconditions,
CRC32C transfer checks and SHA-256 verification. It checks both BigQuery
dataset locations and compares every raw table's schema, source description,
row count and complete row contents against the verified Parquet files.
These readbacks are intentionally practical for this bounded PBMC dataset.

It then runs `dbt build --target bigquery` with explicit project, profile,
release, raw dataset and location settings. Each invocation gets a new
temporary output directory, preventing stale artifacts from being reused.
Exit code zero is required, along with a successful result for every model
and a passing result for every test in the manifest. Missing, skipped,
warning or failed results fail the worker. Manifest and results invocation
IDs must match, and model schemas must match the requested release dataset.

The analytics dataset is
`pbmc_analytics_r_71a4bb4d026366b644c4aaf8bd4b29d8f8f87fe1701ba2e8d6a88ba164b83f7d`.
Terraform provisions this specific dataset before dbt runs; the worker does
not need project-wide dataset creation permissions. Dataset destruction is
protected, and deleting nonempty contents is disabled. Additional releases
will need corresponding datasets and grants before execution.

Unlike the immutable raw release, dbt analytics are rebuildable: repeating
the job runs new queries and replaces its three materialized mart tables.
The three staging models are views. dbt is not a cross-model transaction;
a failed build may leave partial analytics. Only a completed Cloud Run job
with successful tests and durable artifacts is accepted as a successful run.
One task runs per execution; avoid concurrent executions for the same release.

## Resources and permissions

`infra/dbt.tf` and `infra/dbt_job.tf` define:

- Cloud Run job `pbmc-dbt-build`, region `europe-west1`, one task,
  one CPU, 2 GiB memory, 1,200-second task timeout and one task retry.
- Service account `pbmc-dbt-runner`: project-level BigQuery Job User,
  Data Viewer on `pbmc_raw`, and Data Editor on the specific analytics dataset.
- GCS Object Viewer limited by object-prefix conditions to committed releases
  and its own artifact/audit paths. Object Creator is limited to its own paths.
  The worker has no bucket object deletion or raw-table write grant.
- Container `pbmc-warehouse:v0.2.0`, built using `Dockerfile.warehouse`,
  pinned by OCI index digest
  `sha256:ccb29689345a600fa92123eb91363eb023c8bb4d41d474dd0417b8dd0ddc1398`.
  It runs as non-root UID 10001 with the attached service account and no keys.

The profile uses two dbt threads, interactive queries, a 300-second per-query
timeout, one query retry and a 100 MB maximum billed-bytes limit per query.
This is a per-query control, not a total project spending cap.

## Durable evidence and failures

Each attempt produces a structured `cloud_dbt_run` log and a create-only audit
at `gs://pbmc-okf-portfolio-data/runs/cloud_dbt/<run_id>.json`.
Available `manifest.json`, `run_results.json` and `dbt.log` files are saved at
`gs://pbmc-okf-portfolio-data/runs/dbt/<run_id>/`, including on dbt failures.
If processing succeeds but an artifact upload fails, the task still fails.
When processing already failed, its original error is preserved and upload
errors are logged separately. Inspect Cloud Run's terminal status as well as
the processing-status audit: a successful processing audit alone does not
prove artifact delivery completed. A retry creates a new audit directory.

Run the deployed job manually:

```powershell
gcloud.cmd run jobs execute pbmc-dbt-build `
  --project=pbmc-okf-portfolio `
  --region=europe-west1 --wait
```

For a local run from the repository with the warehouse requirements and ADC:

```powershell
$env:PYTHONPATH = 'src'
.venv\Scripts\python.exe -m pbmc_platform.run_cloud_dbt `
  --project pbmc-okf-portfolio `
  --bucket pbmc-okf-portfolio-data `
  --release-id 71a4bb4d026366b644c4aaf8bd4b29d8f8f87fe1701ba2e8d6a88ba164b83f7d
```

## Verification

The full Python suite passed with 89 tests, including 13 new dbt worker tests
covering raw-table tampering, missing commits, wrong locations, dbt failures,
skipped/missing results, invocation mismatch, wrong target schemas, artifact
delivery failures and stale-result prevention. Linux smoke checks verified
non-root execution, dependency consistency and parsing all six models/30 tests.

Live executions on 2026-11-01:

- `pbmc-dbt-build-tnkrd`: successful first build, approximately 65 seconds.
- `pbmc-dbt-build-ngj22`: successful repeat build of the same release.

Independent checks downloaded each run's generation-pinned GCS audit,
manifest, results and log, compared the durable audit with its structured
Cloud Logging entry, and confirmed six successful models and 30 passing tests
in each run's artifacts. BigQuery
readback confirmed 2,700 staging cells, 32,738 staging genes, one staging
summary, one dataset QC mart, four exclusion rows and 2,638 cohort cells.
Every model contained only the requested release. QC totals were 2,638 passes,
62 failures and 6,390,631 counts; exclusion flags were five high-gene and
57 high-mitochondrial cells, with zero low-gene or zero-count cells.
The runtime identity was the dedicated dbt service account. The final
Terraform formatting, validation and drift checks passed with no changes.
The local generated evidence report is `artifacts/cloud_dbt_verification.json`;
it is excluded from Git.

The dbt job is now also invoked by the [complete pipeline workflow](008-pipeline-handoff.md)
after QC and warehouse loading, using the QC execution's exact release ID.
[Full-chain failure/retry drills and recovery](009-pipeline-operations.md) are
verified. The deployed job now uses a wrapper that preserves this dbt build
behavior and publishes an execution-bound receipt for
[cloud knowledge automation](014-cloud-knowledge.md). The scientist assistant
remains a later stage.

## References

- [dbt 1.12 BigQuery setup and permissions](https://docs.getdbt.com/docs/local/connect-data-platform/bigquery-setup?version=1.12).
- [BigQuery dataset listing permission filtering](https://docs.cloud.google.com/bigquery/docs/reference/rest/v2/datasets/list).
