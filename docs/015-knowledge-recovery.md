# Knowledge recovery checks and Stage 5 acceptance

Stage 5.6 checks the knowledge boundary in the cloud after the successful full
four-stage production run documented in [Stage 5.5](014-cloud-knowledge.md).
It adds a separate fault-injection image and isolated jobs/workflow. The production
worker image and workflow are not changed to support these faults.

## Isolation and shared upstream evidence

The suite uses `pbmc-okf-portfolio-drills`, `pbmc_drill_raw` and
`pbmc_drill_analytics_r_<release_id>`. A dedicated `pbmc-drill-knowledge` account
can read those two datasets and read/create objects in the drill bucket. It has
no production dataset or bucket grant and no BigQuery Job User/Data Editor grant.
The wrapper also rejects production resource names before injecting any fault.

`pbmc-drill-dbt-handoff` runs the unmodified production dbt wrapper once against
the isolated data, producing a real successful receipt with six models and 30
passing tests. `pbmc-knowledge-drills` reuses the production template from the
dbt-receipt boundary through knowledge completion. It runs the five knowledge
cases against that same upstream receipt; it does not redundantly rerun QC and
warehouse loading for each case. Those stages have separate live evidence.

The drill image extends the pinned production knowledge image and adds only
`ops/knowledge_drill_worker.py`. Runtime monkeypatches are confined to that image
and guarded resource set. The original Stage 4 drill workflow remains available.

## Acceptance scenarios

| Scenario | Expected workflow result | Required evidence |
| --- | --- | --- |
| Invalid dbt handoff | FAILED | Both attempts reject a run ID that differs from the receipt; no knowledge package or completion receipt |
| Altered draft totals | FAILED | The real validator rejects the altered value on both attempts; failed validation reports persist; no publication |
| Publication interruption | SUCCEEDED after retry | First attempt commits only `knowledge.json`; its version has no completion marker. Retry publishes a complete independently validated version without replacing the first file |
| Task interruption after completion | SUCCEEDED after retry | Retry verifies and reuses the existing execution receipt and package; one validation and one complete package, with unchanged generations |
| Healthy execution | SUCCEEDED | One attempt, all source checks pass, exact 2,638-cell cohort and a committed verified bundle |

Two expected workflow failures are passing test outcomes only when their failure
evidence and absence of publication are verified. A generic infrastructure failure
is not accepted as a successful negative test.

The suite independently checks workflow states, job creator/runtime identities,
Cloud Run attempt markers, durable audits against structured logs, validation
reports and generation-pinned package readback. It snapshots all production bucket
object generations and raw/analytics table metadata before and after the suite.
Those snapshots must match. Isolated release generations and table metadata must
also remain unchanged during the knowledge cases after the dbt preparation run.

## Run and recover

Apply the reviewed isolated drill resources, use the project environment and ADC,
then run from the repository root:

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m ops.run_knowledge_drills
```

The driver saves `artifacts/knowledge_drills/<run>/report.json` as it progresses.
Each case records the workflow/execution IDs before waiting for completion. If
verification fails, inspect that report and the cloud execution before retrying:

```powershell
.\.venv\Scripts\python.exe -m ops.run_knowledge_drills `
  --resume artifacts/knowledge_drills/<run>/report.json
```

Resume requires a failed report and unchanged production snapshots. It reuses the
recorded upstream execution and recorded case workflows; already passed cases
are skipped. Use one suite at a time. This is not a distributed concurrency lock.

When a real production knowledge run fails, inspect its Cloud Run execution,
`runs/cloud_knowledge/` audit, validation reports and publication attempts. A
committed package is usable only through its checked completion marker. A lone
file or a failed validation report does not make a published version. Do not
delete or overwrite conflicting objects to force a retry to pass. Regenerate and
revalidate corrected inputs as a new version, then rerun the workflow.

## Evidence boundaries

The isolated suite tests application failures and task retries. It does not prove
live timeout/cancellation behavior, distributed mutual exclusion, alert delivery,
clinical validity or performance at larger data volumes. Production and isolated
data remain separate; the completed production run is the source for the portfolio
demo, while the drill bucket holds intentional failures and an incomplete version.

## Accepted live results: Stage 5 complete

All five cases passed on **2026-11-04**, from 20:39:09 to 20:47:58 UTC (about
nine minutes including upstream preparation). The shared dbt execution was
`pbmc-drill-dbt-handoff-jbkfj`, run `50993ac3c27543618bbf331dda97e084`, with six
successful models and 30 passing tests.

| Scenario | Workflow execution ID | Observed state | Task attempts | Check |
| --- | --- | --- | --- | --- |
| Invalid handoff | `8736c92e-a7bb-40c9-8e0a-ada1a8ef9804` | FAILED | 0, 1 | Exact run-ID mismatch; zero package objects |
| Altered draft | `0b22517a-93ba-4926-a316-c4f283bfe9f3` | FAILED | 0, 1 | Exact `cells_total` validation mismatch; zero package objects |
| Publication retry | `3df94429-8507-4925-9642-dc823a6c3e4d` | SUCCEEDED | 0, 1 | Interrupted first upload preserved; complete new version published |
| Completion retry | `6eab217a-f8de-44d8-b505-90f81d3e5b63` | SUCCEEDED | 0, 1 | Same committed version reused; one validation, four package objects |
| Healthy | `87682d47-6404-442f-95e4-b84a4fe9f488` | SUCCEEDED | 0 | Independently verified cohort of 2,638 cells |

The intended injected failure reasons were checked against durable audits,
validation reports and Cloud Logging. This check was also applied separately to
the completed suite evidence after the driver's assertions were tightened; no
cloud cases needed to be rerun. All production object generations and table
metadata matched their initial snapshots. Isolated release generations and table
metadata also matched after the upstream preparation step.

The drill image is pinned at
`europe-west1-docker.pkg.dev/pbmc-okf-portfolio/pbmc-containers/pbmc-knowledge-drills@sha256:02888f466cb812acd7cc6da35883f972b6b66a526983cc4a4b2f4d8d3f72bdb4`.
Terraform applied 11 isolated resources without changing or destroying existing
resources. Final formatting and validation passed; the subsequent plan reported
no changes. The full offline suite passed **198 tests**.

The [portable evidence summary](../examples/knowledge-recovery-evidence.json)
contains actual workflow/job identities, attempts, publication IDs and checked
failure reasons. Full local evidence is under
`artifacts/knowledge_drills/20261104T203909Z/report.json`; its SHA-256 is
`9b3d75ac77fa2c660946fa99cdee0b326c709d5dcf2da5ec457ca025e37e237a`.
The same directory contains `reason_verification.json` and the completion-retry
log readback. Generated evidence remains excluded from Git.

Stage 5 is accepted. Stage 6 will add the scientist assistant using a selected,
verified knowledge package as its evidence source.
