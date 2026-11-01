# Loading a committed GCS release into BigQuery from Cloud Run

The warehouse worker consumes an explicit release ID. It does not rerun QC,
compute a producer identity using its own runtime, choose a latest release,
or download the original dataset over HTTPS.

## Input contract and execution

The worker requires `processed/releases/<full SHA-256>/release.json` and all
four referenced output files. It downloads into staging with generation
preconditions and CRC32C transfer verification, validates SHA-256 checksums,
release identity, Parquet keys and QC reconciliation, then promotes the
verified local release. A missing manifest, invalid identity or corrupted
file stops processing before any BigQuery load. An optional local work
directory can reuse a validated local snapshot; Cloud Run uses fresh
temporary storage on every attempt.

The shared download implementation is `cloud_release.py`. QC additionally
requires its own expected producer identity; downstream readers validate the
recorded producer identity against the requested release ID without comparing
it to the reader's different package/runtime environment.

For a local integration run with ADC, use:

```powershell
$env:PYTHONPATH = "src"
python -m pbmc_platform.run_cloud_warehouse `
  --project pbmc-okf-portfolio `
  --bucket pbmc-okf-portfolio-data `
  --release-id 71a4bb4d026366b644c4aaf8bd4b29d8f8f87fe1701ba2e8d6a88ba164b83f7d
```

This command downloads GCS objects, loads/verifies BigQuery tables and writes
cloud audits. It is not a dry run. The same full release ID is used for the
input prefix and all three raw table names. The sparse H5AD matrix is verified
but is not loaded into BigQuery.

## Existing dataset and permissions

The raw dataset `pbmc_raw` must already exist in `europe-west1`. Cloud execution
uses `require_existing_dataset=True`, which reads the dataset and validates
its location without attempting dataset creation. The existing local loader
keeps its earlier default behavior; its new `--require-existing-dataset` flag
opts into this narrower contract.

Terraform references the dataset through a data source; it does not adopt,
recreate or delete it. The dedicated `pbmc-warehouse-runner` identity receives:

- BigQuery Job User at the project, to submit load jobs.
- BigQuery Data Editor on `pbmc_raw`, to create/load and verify its tables.
- GCS Object Viewer restricted to processed releases and warehouse audit
  prefixes, allowing exact object reads rather than broad bucket listing.
- GCS Object Creator restricted to `runs/warehouse/` and
  `runs/cloud_warehouse/`. It cannot replace release files or write raw input.

No service-account key is created. The runtime uses its attached identity.
Future dbt execution will have a separate identity and analytics permissions.

## Container and run records

`Dockerfile.warehouse` packages the Python loader, BigQuery/dbt dependencies
and dbt project. Its Dockerfile-specific ignore file excludes local data,
artifacts, credentials, Terraform state, generated dbt outputs and PDF files.
The default entry point runs `pbmc_platform.run_cloud_warehouse`; the dbt
project is included for the subsequent automation stage, not executed here.

Each invocation writes a structured `cloud_warehouse_run` log. The outer audit
is stored at `runs/cloud_warehouse/<run_id>.json`; detailed loader audits are
stored at `runs/warehouse/<loader_run_id>.json`. Records include the selected
release, input manifest checksum, cloud execution name when available,
BigQuery job IDs, table row counts, reuse flags and failure information.

Audits are uploaded on ordinary Python success and failure. Audit delivery
failure after a successful load produces a nonzero exit. If loading and audit
delivery both fail, the original load error is preserved and a separate
audit-upload error event is printed. Hard termination can still prevent final
audits; inspect the platform execution status and logs as well.

Tables remain immutable under the loader's contract: `WRITE_EMPTY` populates
empty tables, and nonempty tables must match the expected schema, source
description and every data row. Reruns verify/reuse completed tables. A partial
failure resumes without appending duplicate rows. IAM Data Editor is broader
than this application contract; the code itself does not overwrite or delete
existing release tables.

## Deployed job

`infra/warehouse_job.tf` defines `pbmc-warehouse-load` in `europe-west1`, with
one task, 1 vCPU, 2 GiB memory, a 900-second attempt timeout and one retry.
Its image is pinned to the verified v0.1.0 digest:

```text
sha256:bf9dfddaf93d01997fbcf127363193759856545310acafe40ab0e0ef891806bb
```

The `warehouse_release_id` Terraform variable selects the explicit committed
release; its default is the verified Linux QC release shown above. Review and
apply a plan when changing that standalone selection. Dynamic handoff from
QC will be added when extending Workflows.

Run the deployed job with:

```powershell
gcloud.cmd run jobs execute pbmc-warehouse-load `
  --project=pbmc-okf-portfolio `
  --region=europe-west1 `
  --wait
```

## Verification and remaining integration

Offline tests cover consumer/producer runtime separation, explicit IDs,
manifest absence, corrupted data, mismatched identities, existing-dataset
access without creation, location mismatch, partial load recovery, duplicate
prevention and audit failures. Existing QC replay tests continue to exercise
the shared download helper.

The full Python suite passed **76 tests**. The image smoke test confirmed a
non-root runtime, valid imports, dbt project availability and no broken
dependency requirements. The deployment added eight Terraform resources
without changing or destroying existing infrastructure.

Live verification on 2026-11-01 (Europe/Berlin):

| Execution | Result | Warehouse behavior |
| --- | --- | --- |
| `pbmc-warehouse-load-2gbwm` | Succeeded | Three new load jobs completed |
| `pbmc-warehouse-load-kl9fq` | Succeeded | All tables verified and reused; no load jobs submitted |

The independently verified row counts were 2,700 cells, 32,738 genes and one
dataset summary. Both runs consumed the same committed Linux release. The
source checksums, full BigQuery table contents, source-contract descriptions,
runtime identity and load-job identities were verified. Both durable GCS
audits matched their structured logs. The final Terraform plan reported no
changes. Local evidence is in `artifacts/cloud_warehouse_verification.json`
(excluded from Git).

This implements Step 4, substep 1. The verified loader is now also invoked
by the [complete pipeline workflow](008-pipeline-handoff.md), which passes
the release ID from the successful QC execution's receipt.

References:

- [BigQuery access control](https://docs.cloud.google.com/bigquery/docs/access-control).
- [Docker build context and ignore files](https://docs.docker.com/build/concepts/context/).
