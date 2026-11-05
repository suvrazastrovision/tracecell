# Cloud knowledge automation (Stage 5.5)

A workflow run now performs QC, warehouse loading, dbt, then knowledge generation,
independent validation and publication. The knowledge stage runs as
`pbmc-knowledge-build` in `europe-west1`. This adds no schedule or automatic trigger.

```text
QC -> verified QC receipt -> warehouse -> dbt + tests
  -> verified dbt execution receipt
  -> generate draft -> independently validate -> publish verified bundle
  -> knowledge completion receipt -> workflow success + publication URI
```

## Execution-bound dbt handoff

The existing dbt implementation is wrapped by `run_dbt_handoff`; its build and
test behavior is retained. After the original dbt worker has uploaded its audit
and artifacts, the wrapper checks them and creates:

```text
runs/handoffs/dbt/<dbt_execution>.json
```

The receipt binds project, bucket, release, raw/analytics datasets, location and
Cloud Run execution to a specific cloud dbt run and invocation. It records the
audit, manifest and result object paths, generations and SHA-256 values.
If the same Cloud Run execution retries after creating this receipt, it verifies
the existing evidence and reuses it instead of rebuilding dbt. A conflicting
receipt or changed artifact fails the task. Wrapper audits are under
`runs/dbt_handoff/`.

The workflow reads the receipt with a generation precondition, checks its
execution/release binding and passes that exact dbt run, execution name and
receipt generation to the knowledge job. There is no latest-run search.

## Knowledge job and durable evidence

The knowledge worker checks the dbt receipt and every bound artifact before
generating data. It runs the same generator, independent validator and create-only
publisher used in Steps 5.2–5.4. A failed validation stops publication. The worker
uploads drafts, validation reports and its outer audit to these restricted paths:

- `runs/knowledge_generation/<attempt_id>/`
- `runs/knowledge_validation/<attempt_id>/`
- `runs/cloud_knowledge/<attempt_id>.json`
- `runs/knowledge_publication/<publication_attempt_id>.json`

The verified bundle is published under the existing
`knowledge/releases/<release_id>/<package_id>/` protocol. Only after all processing
and durable artifact uploads succeed does the worker create
`runs/handoffs/knowledge/<knowledge_execution>.json`.

The workflow reads and checks this completion receipt before returning success.
Its result includes the four execution names, selected dbt run, receipt URIs and
generations, package ID and publication URI.
Receipt conditions use separate branches to stay within the workflow service's
400-character expression limit; the longest rendered expression is 250 characters.

## Runtime and permissions

The new image extends the previously verified warehouse/dbt image at digest
`ccb29689345a600fa92123eb91363eb023c8bb4d41d474dd0417b8dd0ddc1398`.
It retains its dependency runtime, including `jsonschema 4.26.0`, and copies the
current source and package schema. Both dbt and knowledge jobs use the new pinned
image at `078bb88fff58ab7147b0188e1b1be15ed10c85dceefab1adb738058ece510f95`.
The container runs as UID 10001; its build context excludes credentials and local
artifacts. The QC and warehouse job images remain the same.

`pbmc-knowledge-runner` has BigQuery Data Viewer on the selected raw and analytics
datasets. It has no BigQuery Job User or Data Editor grant: reads use table APIs,
and it cannot create SQL jobs or change warehouse data through these grants.
GCS permissions allow reading the release, dbt evidence, receipts and its own
outputs, plus creating objects only under its knowledge/audit/receipt prefixes.
The dbt account gains receipt read/create permissions in addition to its existing
artifact permissions. The workflow account gains knowledge-job execute-with-
overrides/view permissions and read access to dbt/knowledge receipt prefixes.

The knowledge job has one task, one CPU, 2 GiB RAM, an 1800-second task timeout
and one task retry. Its workflow connector waits up to 3900 seconds. The job's
default upstream identifiers intentionally fail validation; the workflow must
supply actual execution-bound arguments.

## Retry and operational boundary

When a knowledge completion receipt already exists for this execution, the
worker verifies the published marker, generations, checksums and bundle identity
and reuses it. Completed execution replay does not regenerate or revalidate.
Retries before that receipt exists may create another independently validated
package version, because temporary local validation state is not a distributed
checkpoint. Previously published versions are preserved. An audit or receipt
upload failure makes the task fail even if a verified bundle was already committed.

Run one pipeline operation per release at a time. There is no distributed lock,
cross-service transaction, live timeout/cancellation fault proof, scheduler or
alert delivery in this step. Cancelling a workflow does not automatically cancel
its Cloud Run tasks. The isolated Stage 4 drill workflow retains its existing
three-stage behavior; it does not exercise the new knowledge stage.

## Run and inspect

```powershell
gcloud workflows execute pbmc-qc-workflow `
  --project=pbmc-okf-portfolio --location=europe-west1
```

Inspect the workflow execution in the GCP console. Its final result provides
`knowledge_publication_uri`. Follow the receipt and publication references in
Cloud Storage, and download `report.md` for VS Code Markdown preview. The stored
validation audit contains the recalculated metrics, full cohort count and source
versions used to verify the package.

## Checks

The 11 new offline worker tests cover receipt identity/generation/checksums,
completed execution reuse, failed validation evidence, blocked publication and
recovery from outer-audit or receipt upload failure. The full suite passes 187
tests. The Linux image smoke check imports both workers as UID 10001 and accepts
the existing verified bundle under read-only mounts.

Terraform validation succeeds. The reviewed plan adds eight new resources,
replaces three scoped IAM bindings and updates three existing resources. No data
resources are deleted; the isolated drill workflow is unchanged.

## Live deployment verification

Workflow execution `991eec54-fee3-4ad7-be64-fb7b778088c2` completed successfully on
2026-11-04. Independent verification checked the creator and runtime service
account of all four executions, their completion order, exact release arguments,
the new image digest, dbt receipt generation and the knowledge input overrides.

| Stage | Cloud Run execution |
| --- | --- |
| QC | `pbmc-qc-v79hn` |
| Warehouse | `pbmc-warehouse-load-54x8r` |
| dbt | `pbmc-dbt-build-2f26k` |
| Knowledge | `pbmc-knowledge-build-mrlpq` |

The selected cloud dbt run was `e6a7f3d6f4194586b66399a763925cd1`; its downloaded
artifacts contained six successful models and 30 passing tests. The independently
validated package recorded 2,700 cells, 2,638 passing, 62 failing and 32,738
features, including exact cohort membership. All six validation groups passed
against eight GCS source objects and six BigQuery tables.

Published package ID:
`e2c908da59111f5e12ebac24804e526f938802a7bbcbd6020bb7cd4cb0a2e04f`.
Its completion marker is under the selected release at
`knowledge/releases/<release_id>/<package_id>/publication.json` in
`pbmc-okf-portfolio-data`.

The verification downloaded both execution receipts, the committed package and
validation report, the successful publication attempt audit and the cloud worker
audit. It checked pinned generations, SHA-256 values, bundle identity, matching
dbt inputs and the original source generations. Local evidence is saved in
`artifacts/cloud_knowledge_workflow_verification.json`, with a readable package
copy under `artifacts/cloud_knowledge_published/<package_id>/`. These generated
artifacts are excluded from Git.

The final Terraform format/validation checks passed and the post-deployment plan
reported no changes. The workflow template's initial overlong receipt expressions
were split into bounded branches before the successful deployment; no runtime
failure was accepted as a passing test.

Stage 5.6 subsequently passed all five isolated knowledge failure/recovery cases.
See [the recovery guide](015-knowledge-recovery.md) for the accepted execution IDs,
fault reasons and production-isolation evidence. The original Stage 4 workflow
remains unchanged; the new suite checks the dbt-to-knowledge boundary separately.
