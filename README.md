## scRNA-seq Knowledge Platform

A GCP data engineering portfolio project for reproducible scRNA-seq data
processing, quality controls, analytics, and Open Knowledge Format (OKF).

The platform turns a public PBMC count matrix into traceable SQL tables and a
verified knowledge package and a bounded scientist-facing assistant. **Stages 1–6
are complete** for this deterministic portfolio release. The authenticated cloud
assistant accepts typed questions across six evidence topics; grammar routing
passed 60 cloud regression cases without a language model. **Stage 7 operations
are deployed and verified; actual-money reporting awaits billing-export data.**

## Data flow

```mermaid
flowchart LR
    Source[Public PBMC3k counts] --> QC[Cloud Run: ingestion and QC]
    QC --> Release[GCS: committed release]
    Release --> Load[Cloud Run: warehouse loader]
    Load --> Raw[BigQuery: raw tables]
    Raw --> dbt[Cloud Run: dbt build and tests]
    dbt --> Analytics[BigQuery: analytics]
    Analytics --> Knowledge[Cloud Run: generate, validate, publish]
    Knowledge --> Package[GCS: verified knowledge package]
    Package --> Snapshot[Pin and bundle reviewed snapshot]
    Snapshot --> Assistant[Cloud Run: authenticated evidence assistant]
```

Google Cloud Workflows sequences the four jobs. Execution receipts bind each
downstream stage to the selected release and successful upstream run. Terraform
manages infrastructure and IAM; Artifact Registry holds digest-pinned images.
The assistant snapshot is selected and bundled during its separate image build;
new pipeline publications do not automatically change its answers.

## What this demonstrates

- Reproducible inputs and QC: a checksum-pinned count matrix, recorded policy,
  code/runtime identity and immutable release publication with a final marker.
- Safe warehouse loading: release-specific tables, explicit schemas, complete
  readback checks and retries that reuse matching rows without appending duplicates.
- Tested analytics: six dbt models and 30 data tests, with excluded cells retained
  for audit and a separate passing-cell cohort.
- Evidence-backed knowledge: schema checks, independent calculations from the
  count matrix, exact cohort comparison, versioned publication and durable audits.
- Scoped service accounts and isolated cloud fault tests, with documented limits.

## Dataset and scientific boundary

The initial release contains **2,700 cells and 32,738 features** from one healthy
donor. Under the recorded QC policy, **2,638 cells pass and 62 fail**. The starting
input is an unnormalized count matrix, not FASTQ sequencing reads. This pipeline
does not infer cell types or support disease comparisons.

Dataset attribution: **10x Genomics, CC BY 4.0**. Source details and checksums are
recorded in [the dataset configuration](configs/dataset.yaml) and
[project purpose](docs/001-project-purpose.md).

Here, OKF means this project's versioned JSON knowledge package. It does not
claim conformance to an external Open Knowledge Format standard. The
[example](examples/pbmc-knowledge-v1.json) is illustrative, with explicitly
fictional evidence locations; it is not a verified cloud result.

## Main GCP resources

| Resource | Function |
| --- | --- |
| Cloud Storage | Raw cache, committed releases, execution receipts, knowledge versions and audits |
| BigQuery | Raw release tables and dbt analytics |
| Cloud Run Jobs | QC, warehouse loading, dbt and knowledge processing |
| Cloud Run service | Authenticated assistant serving a selected verified snapshot |
| Cloud Scheduler | Weekly guarded pipeline trigger |
| Cloud Monitoring and Pub/Sub | Failure policies and a private notification inbox |
| Workflows | Ordered execution and receipt checks |
| Artifact Registry | Digest-pinned worker images |
| IAM | Separate runtime identities and scoped access |
| Cloud Logging | Structured processing and failure evidence |
| Terraform GCS backend | Shared infrastructure state |

## Review and reproduce

- [Try the assistant](docs/017-assistant-foundation.md): run a supported question
  locally using the archived verified package, without GCP credentials or an API key.
- [Open the browser interface](docs/018-assistant-browser.md): choose a question
  and view its answer, sources and limitations at `http://127.0.0.1:8765`.
- [Typed questions and routing evaluation](docs/019-assistant-question-routing.md):
  supported everyday phrasings, abstention boundaries and the 60-case evaluation.
- [Cloud assistant and Windows access](docs/020-cloud-assistant.md): authenticated
  deployment, runtime permissions, proxy instructions and accepted live results.
- [Operations and recovery](docs/021-operations.md): weekly scheduling, atomic
  concurrency guard, real failure-notification delivery and manual recovery.
- [Actual cost reporting](docs/022-actual-cost-reporting.md): prepared billing
  dataset, required export setup and the tested reporter; no invented zero costs.
- [Portfolio walkthrough](docs/016-portfolio-walkthrough.md): a short manual demo
  and interview explanations.
- [Archived verified report](examples/pbmc-verified-report.md): the exact readable
  output from the successful Stage 5 cloud run, viewable without GCP access.
- [Cloud knowledge architecture](docs/014-cloud-knowledge.md): deployment,
  permissions, retry boundaries and a real successful execution.
- [Knowledge recovery checks](docs/015-knowledge-recovery.md) and
  [earlier pipeline drills](docs/009-pipeline-operations.md): expected failures,
  recovery evidence and production-isolation checks.
- [Archived recovery results](examples/knowledge-recovery-evidence.json): actual
  workflow IDs, attempts and verified fault reasons for all five knowledge cases.
- [Knowledge contract](docs/010-knowledge-contract.md),
  [independent validation](docs/012-knowledge-validation.md) and
  [publication protocol](docs/013-knowledge-publication.md).

For offline tests on Python 3.11, from PowerShell at the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-knowledge-dev.txt -r requirements-warehouse-dev.txt
python -m pytest -q
```

The latest full run passed **352 tests**. Tests use generated fixtures and fake cloud services. They do not need GCP
credentials. Cloud deployment and live drill commands require the caller's own
ADC and appropriate permissions; no credentials or service account keys are
included. Generated reports are under the ignored `artifacts/` directory; the
documentation records evidence IDs and explains how to reproduce them.

## Remaining work and limits

Stages 7.1–7.3 provide weekly scheduling, a distributed concurrency lock and
verified private failure notifications. Stage 7.4's dataset and cost reporter are
ready; exported billing rows are still required for actual money totals.
Model-based language support, independent freshness alarms, human notification
receipt, cost-saving claims and live timeout/cancellation tests of actual Cloud
Run data tasks remain outside the accepted evidence. Administrative direct job
runs can bypass the workflow guard. A workflow cancellation does not automatically cancel Cloud Run tasks.
The full readback strategy is intentional for this bounded portfolio dataset;
larger datasets would need a different verification and cost strategy.
