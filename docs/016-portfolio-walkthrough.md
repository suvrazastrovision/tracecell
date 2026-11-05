# Portfolio walkthrough: PBMC knowledge platform

## Explain the project in one minute

This project takes a public single-cell count matrix, records how it was processed,
checks every cell against a versioned QC policy, loads traceable BigQuery tables
and builds tested dbt analytics. It then creates a small knowledge package whose
facts are independently checked against the preserved count matrix and warehouse rows.
Every cloud stage uses a separate service account and passes an execution-bound
receipt to the next stage.

The result is a reproducible, auditable data pipeline and verified context for a
scientist-facing assistant. A local foundation now answers six supported questions
from the verified package in the terminal, [local browser interface](018-assistant-browser.md)
or [authenticated Cloud Run service](020-cloud-assistant.md). It uses evaluated
grammar routing rather than a language model. The
knowledge format is project-defined JSON, not a claim of external OKF compliance.

## A short manual demo

1. Open [the README diagram](../README.md#data-flow). Explain the four Cloud Run
   jobs and why GCS, BigQuery and Workflows have different responsibilities.
2. Open `configs/dataset.yaml` and `configs/qc.yaml`. Show the source checksum,
   dataset version and explicit thresholds. The pipeline starts from counts,
   not FASTQ reads.
3. In GCP Workflows, open `pbmc-qc-workflow` in `europe-west1` and select successful
   execution `991eec54-fee3-4ad7-be64-fb7b778088c2`. Show the ordered job steps,
   receipt checks and final publication URI. This is recorded evidence; starting
   another run is optional and creates another version.
4. In BigQuery, inspect the release-specific raw cells table and analytics
   dataset. Explain that all 2,700 cells remain auditable, while the cohort has
   only the 2,638 passing cells. The 62 failures were labelled and retained.
5. In the bucket, follow the workflow result's `knowledge_publication_uri`.
   Show `publication.json`, `knowledge.json`, `report.md` and `validation.json`.
   Explain that the marker is written last and records checksums and generations.
6. Download `report.md` and preview it in VS Code with **Ctrl+Shift+V**. The local
   copy from the successful run is under
   `artifacts/cloud_knowledge_published/e2c908da59111f5e12ebac24804e526f938802a7bbcbd6020bb7cd4cb0a2e04f/`.
   The [archived report](../examples/pbmc-verified-report.md) is an exact copy for
   reviewers without cloud access; its SHA-256 is
   `2cb9c83c74c27e0f0eaa855e9cebeabd3fc4f802853d168e0fd6e12fcdc0d8f9`.
7. Open [the recovery guide](015-knowledge-recovery.md) and its generated evidence
   report. Show an expected failed workflow and its validation audit, then a
   retry that recovered. Explain why no incomplete package can pass the marker
   and validation checks.

## Useful interview discussions

| Design choice | Reason and tradeoff |
| --- | --- |
| Release ID includes configuration, code and runtime identity | Results can be traced to a precise producer; changing the environment can produce another release |
| Retain failed cells | QC decisions remain explainable and reversible; passing cohorts are separate models |
| Load empty tables and reuse exact matches | Retries do not append duplicate rows; a partial multi-table load can finish safely |
| Compare complete rows and independently recompute QC | Strong evidence for this small dataset; full readback is not a general large-scale verification strategy |
| Execution receipts instead of a latest pointer | Each stage consumes the exact upstream run, even when historical runs exist |
| Final GCS publication marker | Readers distinguish completed versions from partial uploads; this is not a cross-object transaction or retention lock |
| Read-only knowledge service account | Generating explanations cannot change the warehouse through its assigned permissions |
| Separate fault-injection image and resources | Recovery can be demonstrated without modifying production data or enabling production fault flags |

## Make claims supported by evidence

You can demonstrate reproducibility, versioned QC, BigQuery/dbt integration,
execution-bound orchestration, create-only publication, least-privilege grants,
weekly guarded scheduling, delivered private failure notifications and tested
recovery on a public bounded dataset. Point to the documented execution
IDs and repeatable tests rather than describing the system as production-ready.

Do not claim disease inference, inferred cell types, a model-based scientist assistant,
independent freshness/SLO alarms, human receipt of notifications, measured cost savings,
live timeout/cancellation recovery or large-scale performance. Those are outside
the evidence currently collected.

## Current and next stages

Stages 1–6 are complete for the bounded deterministic release, including all five
isolated knowledge recovery checks and 60 cloud assistant cases. The full project
suite now passes 352 tests. The [archived recovery results](../examples/knowledge-recovery-evidence.json)
record actual execution IDs and checked failure reasons. Stage 6 is the scientist
assistant, with [the local foundation](017-assistant-foundation.md) and
[browser interface](018-assistant-browser.md) and
[evaluated question routing](019-assistant-question-routing.md) and
[authenticated cloud deployment](020-cloud-assistant.md) now complete;
Stage 7's [schedule, lock and private alerts](021-operations.md) are verified;
[actual cost reporting](022-actual-cost-reporting.md) awaits billing-export data.
