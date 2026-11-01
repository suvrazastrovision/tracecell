# Publishing QC releases to Cloud Storage

The uploader publishes an existing, validated local release to the private
pipeline data bucket. It uses Application Default Credentials on a developer
machine. The Cloud Run wrapper uses its attached runtime service account.
Do not copy credential files into the container image.

## Validate before uploading

From the repository root, with the virtual environment activated:

```powershell
$env:PYTHONPATH = "src"
python -m pbmc_platform.publish_gcs `
  --project pbmc-okf-portfolio `
  --bucket pbmc-okf-portfolio-data `
  --dry-run
```

The dry run validates the release identity, all four output checksums, Parquet
keys and QC summary reconciliation. It displays the destination objects
without accessing GCS or requiring credentials. Omit `--dry-run` to upload.
By default the local `data/processed/current.json` selects the release; use
`--release-id <full SHA-256>` to select a specific release explicitly.

## Publication contract

Objects are stored under:

```text
gs://pbmc-okf-portfolio-data/processed/releases/<release_id>/
  cells.parquet
  genes.parquet
  dataset_summary.parquet
  counts.h5ad
  release.json
```

Every upload uses `if_generation_match=0` to prevent replacing an existing
live object, including when concurrent publishers race. CRC32C verifies
transfer integrity. Both new and existing objects are downloaded with a
generation precondition and compared with the expected SHA-256; custom object
metadata is not trusted as proof of integrity. This full readback is suitable
for the bounded PBMC3k portfolio dataset and incurs download operations and,
depending on the client location, network charges.

The manifest is uploaded last, after verification of all four data files.
Consumers must require `release.json`, verify its identity and the checksums
of the referenced files, and never treat a prefix listing as a completed
release. Returned object generations identify the versions verified by this
publication attempt. Objects remain subject to bucket IAM; this is an
application publication contract, not a bucket retention lock.

An interrupted initial upload can leave partial objects without a manifest.
Rerun the same command to verify and reuse existing bytes, then upload missing
objects. A mismatch fails without overwriting or deleting the conflicting
object. Preserve conflicting data for investigation before restoring a
verified release. Changing a release ID to bypass a conflict is not a repair.

Each invocation writes a local audit record in `artifacts/storage_runs/`,
including dry runs and Python failures. Hard process termination may prevent
that record from being written. This uploader does not publish a global
`current.json`, upload raw inputs or run audits to GCS, or run QC itself.
The cloud entry point below adds raw input caching, execution and durable
audits. The existing Docker image must be rebuilt to include the new dependency
and entry point before using it in the cloud.

## Cloud QC entry point

The cloud wrapper combines ingestion, QC and release publication:

```powershell
$env:PYTHONPATH = "src"
python -m pbmc_platform.run_cloud_qc `
  --project pbmc-okf-portfolio `
  --bucket pbmc-okf-portfolio-data `
  --work-dir artifacts/cloud-qc
```

This command accesses GCS and can download the checksum-pinned source over
HTTPS. If `--work-dir` is omitted, each invocation uses a fresh temporary
directory, appropriate for an ephemeral Cloud Run worker. A supplied work
directory supports only one writer at a time.

The source is cached as `raw/<source_sha256>/<source_filename>`. Existing cloud
inputs are downloaded with generation and SHA-256 checks; corruption fails
the run rather than silently falling back to HTTPS. New raw objects use the
same create-only, readback-verified publication as release objects.

The wrapper computes the expected release identity for its actual runtime.
If a completed cloud release exists, its manifest and four outputs are
downloaded into staging, verified for identity, checksums, keys and summary
reconciliation, then promoted locally. QC reuses that verified release.
This preserves published bytes even if regenerating an HDF5 container might
produce different bytes. An invalid committed release fails rather than
triggering a replacement. If no committed release exists, QC computes a new
release and the uploader commits its manifest last. Partial prior uploads
are reused only if their bytes match; conflicts still fail safely.

Every attempt prints a structured `cloud_qc_run` log event. The wrapper uploads
its run record to `runs/cloud_qc/<run_id>.json` and any newly generated detailed
QC records to `runs/qc/<qc_run_id>.json`. Release data is shared on replay;
run IDs and audit records remain distinct. The returned `audit_uri` points to
the cloud run record, while `publication` includes verified object generations.

If processing fails, audit upload is attempted before returning a nonzero
exit. If authentication, networking or bucket permissions also prevent audit
upload, the original processing error is preserved and an additional
`cloud_qc_audit_upload_failed` event is printed to stderr. If processing
succeeds but audit upload fails, the command still exits nonzero. The
`cloud_qc_run` event describes processing status before audit delivery;
check exit status and audit-upload error events to assess full success.
Abrupt termination can prevent final audits; platform execution logs remain
necessary. Windows and Linux runtimes can produce different release IDs.

## Cloud runtime identity

`infra/runtime.tf` enables the IAM API and defines `pbmc-qc-runner` as a
dedicated runtime service account. Bucket-scoped Object Viewer and Object
Creator grants allow reading and creating pipeline objects. These grants do
not allow deleting or overwriting existing objects and do not grant access to
the Terraform state bucket. IAM membership resources add these grants without
replacing other bucket members. No service account key is created; the Cloud
Run job will use its attached service account through ADC.

Reference: [Cloud Storage IAM roles](https://docs.cloud.google.com/storage/docs/access-control/iam-roles).

## Cloud Run Job

`infra/cloud_run.tf` enables the Cloud Run API and defines the `pbmc-qc` job in
`europe-west1`. Its image is pinned to the uploaded v0.2.0 digest:

```text
sha256:f5eea9f5f0e0ac564c0ecedcb9e562288eec5507690828570fc065264302ae93
```

The job overrides the image's local-QC entry point to run
`python -m pbmc_platform.run_cloud_qc` with the project and pipeline bucket.
It uses the dedicated runtime service account, one task, one vCPU, 2 GiB of
memory, a 900-second per-attempt timeout and at most one retry. Parallelism
is one within an execution; independent executions can still overlap.
Application create-only preconditions protect shared published objects.

Applying this configuration creates the job definition without executing it.
There is no schedule. After applying the reviewed Terraform plan, execute it
manually:

```powershell
gcloud.cmd run jobs execute pbmc-qc `
  --project=pbmc-okf-portfolio `
  --region=europe-west1 `
  --wait
```

Confirm the platform execution succeeds and inspect its structured logs and
`runs/cloud_qc/` audit record. A first Linux execution can create a different
release from the existing Windows run. A later execution using the same
image/configuration should report `qc_reused: true`,
`restored_from_cloud: true` and `publication.reused: true`. Each attempt uses a
new temporary work directory. Job success does not run the warehouse/dbt
stages; those are separate steps. Deletion protection is disabled for this
portfolio job so it can be removed after use; the data buckets remain
protected by their Terraform lifecycle settings.

Reference: [Create Cloud Run jobs](https://docs.cloud.google.com/run/docs/create-jobs).

## Live Cloud Run verification

Verified on 2026-11-01 (Europe/Berlin) in `pbmc-okf-portfolio`, `europe-west1`:

| Execution | Result | Release reused |
| --- | --- | --- |
| `pbmc-qc-sdnj7` | Completed successfully | No; first cloud computation |
| `pbmc-qc-n47zq` | Completed successfully | Yes; restored on a fresh worker |

Both executions used Linux release
`71a4bb4d026366b644c4aaf8bd4b29d8f8f87fe1701ba2e8d6a88ba164b83f7d`.
The source and all five published files were downloaded at their recorded GCS
generations and their SHA-256 checksums verified. Release identity, Parquet
keys and QC summary reconciliation passed. Both durable cloud audits matched
their structured Cloud Logging records. The replay preserved every published
object generation and reported QC, restoration and publication reuse.

Measured totals: 2,700 cells, 32,738 features, 2,638 QC passes, 62 failures,
and 6,390,631 counts preserved. Both executions completed without a task retry.
The local evidence report is `artifacts/cloud_run_verification.json`; artifacts
are excluded from Git. Workflow orchestration is documented in
[005-workflow-orchestration.md](005-workflow-orchestration.md).

## Verification

Offline tests cover manifest-last publication, unchanged generations on
replay, interrupted upload recovery, remote conflicts, corrupted readback,
local corruption, dry runs, and audit records. Cloud wrapper tests cover
fresh-worker restoration, source caching, missing or corrupted committed
releases, generation changes, processing failures and audit delivery errors.

Reference: [Google Cloud Storage Blob API](https://docs.cloud.google.com/python/docs/reference/storage/latest/google.cloud.storage.blob.Blob).
