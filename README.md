# scRNA-seq Knowledge Platform

A Google Cloud data engineering portfolio project that turns public single-cell
RNA sequencing counts into checked tables, a readable report and an evidence assistant.

## Why this exists

A dataset alone does not explain which cells passed quality checks or where a
reported number came from. This pipeline records the input, quality rules,
processing steps and evidence so its results can be checked and reproduced.

The current example is **PBMC3k: 2,700 cells and 32,738 gene features from one
healthy donor**. Under the recorded rules, **2,638 cells pass and 62 fail**.
Source: 10x Genomics, **CC BY 4.0**; details are in
[the dataset configuration](configs/dataset.yaml).

## Complete data pipeline

Solid arrows show processing and delivery. Dotted arrows show orchestration,
evidence inputs and supporting services. Workflows runs the four jobs in order
and checks their completion evidence before continuing.

```mermaid
flowchart TD
    subgraph Control["Start and coordinate"]
        Schedule["Cloud Scheduler: Monday 09:00 Europe/Berlin"] --> Workflow["Workflows: pbmc-qc-workflow"]
        Manual["Manual trigger"] --> Workflow
        Workflow --> Lock{"Acquire GCS lock"}
        Lock -->|Already held| Skip["Skip overlapping run"]
    end

    subgraph Processing["Four sequential Cloud Run Jobs"]
        Source["Public PBMC3k .h5ad: unnormalized counts"] --> QC["1. pbmc-qc: download, checksum and cell quality checks"]
        Config["Dataset identity and recorded QC policy"] -.-> QC
        QC --> Release["GCS committed release: counts.h5ad, cell and gene Parquet, summary and manifest"]
        Release --> QCReceipt["Execution-specific QC receipt: verify release and object generation"]
        QCReceipt --> Load["2. pbmc-warehouse-load: validate, load or reuse matching tables"]
        Load --> Raw["BigQuery pbmc_raw: release-specific cells, genes and summary"]
        Raw --> Dbt["3. pbmc-dbt-build: build six models and run 30 data tests"]
        Dbt --> Analytics["BigQuery release-specific analytics: staging, QC summary, exclusions and passing cohort"]
        Dbt --> DbtEvidence["GCS dbt artifacts, audit and execution-specific receipt"]
        DbtEvidence --> Handoff["Verify successful dbt execution, invocation and receipt generation"]
        Handoff --> Knowledge["4. pbmc-knowledge-build: generate draft OKF"]
        Analytics --> Knowledge
        Knowledge --> Validate["Independently validate schema, source counts, raw rows, analytics and exact cohort"]
        Release -.-> Validate
        Raw -.-> Validate
        DbtEvidence -.-> Validate
        Validate --> Publish["GCS versioned package: knowledge.json, report.md and validation.json; publication.json written last"]
        Publish --> FinalReceipt["Verify knowledge completion receipt and publication identity"]
    end

    Lock -->|Acquired| QC
    Workflow -.-> QCReceipt
    Workflow -.-> Handoff
    Workflow -.-> FinalReceipt
    FinalReceipt --> Success["Workflow success: release exact lock generation"]

    subgraph Serving["Separate assistant build and deployment"]
        Publish --> Review["Review and pin selected publication; verify and bundle snapshot"]
        Review --> Assistant["Authenticated Cloud Run assistant or local browser app"]
        Assistant --> Answers["Supported questions: facts, evidence references and limitations"]
    end

    subgraph Operations["Infrastructure, failures and recovery"]
        Terraform["Terraform: infrastructure and IAM; remote state in GCS"] -.-> Workflow
        Images["Artifact Registry: digest-pinned container images"] -.-> QC
        Images -.-> Load
        Images -.-> Dbt
        Images -.-> Knowledge
        Workflow -.->|Pipeline failure| Retain["Retain lock; inspect owner and running tasks before recovery"]
        Retain --> Logs["Cloud Logging and Monitoring failure policies"]
        Schedule -.->|Delivery failure| Logs
        Logs --> Inbox["Private Pub/Sub alert inbox"]
    end
```

The assistant keeps its selected snapshot until it is rebuilt and redeployed.
Billing export is a separate reporting path: its BigQuery dataset and cost
reporter are ready, but actual cost records are still pending.

| Step | In simple words | Tool |
| --- | --- | --- |
| 1. Get the data | Download the expected file and check its fingerprint to detect changes. | Python |
| 2. Check quality | Count genes and mitochondrial counts per cell, record pass/fail reasons, and save all cells in a versioned release. | Python, Cloud Storage |
| 3. Load tables | Store cell/gene metadata and summaries; verify matching tables before reusing them on retries. The expression matrix stays outside BigQuery. | Cloud Run Job, BigQuery |
| 4. Build analytics | Turn raw metadata into six useful SQL models and run 30 data tests. | Cloud Run Job, dbt |
| 5. Publish knowledge | Independently check results against source data, then publish JSON facts, a report and validation evidence. | Cloud Run Job, Cloud Storage |
| 6. Answer questions | Serve a selected verified package and show the evidence behind supported answers. | Local Python app or authenticated Cloud Run service |

**OKF** is this project's name for its versioned JSON knowledge package. It
contains facts, source references and limitations; it is not an external standard.
The assistant uses fixed rules, without a language model. New publications require
a separate assistant build and deployment before they change its answers.

## How the cloud pipeline is managed

- **Workflows** runs four jobs in order: QC, loading, dbt and knowledge. It checks
  receipts linking each stage to the correct release and successful run.
- **Cloud Scheduler** starts the workflow on Mondays at 09:00 Europe/Berlin.
- A **Cloud Storage lock** skips overlapping workflow runs. Failed or cancelled
  runs retain the lock for inspected recovery; cancelling a workflow does not
  automatically stop its Cloud Run tasks.
- **Terraform** defines infrastructure and access permissions. **Artifact
  Registry** stores the container images used by the jobs.
- **Logging, Monitoring and a private Pub/Sub inbox** record failures and deliver alerts.

## Run locally: easiest demo

Install **Python 3.11**, open this repository folder in VS Code, and run these
commands in its **PowerShell terminal**. No GCP login or AI API key is needed
for the bundled assistant demo.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-knowledge.txt
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m pbmc_platform.assistant_web
```

Open **http://127.0.0.1:8765** and ask **How many cells passed QC?**
Expected result: **2,638 of 2,700 cells passed QC; 62 failed.**
Keep the terminal running; press **Ctrl+C** to stop. When reopening the project,
run the last two commands again. These commands use the virtual environment
without requiring PowerShell activation.

This demo reads an archived verified result; it does not rerun the cloud pipeline.
You can also read the [verified report](examples/pbmc-verified-report.md) directly.

## Run the processing

To download the configured public input and run QC locally, use the environment
above and run:

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m pbmc_platform.run_qc
```

The first run needs internet access. Outputs are saved under `data/processed/`
and audit records under `artifacts/`. This runs the acquisition/QC stage only;
it does not load BigQuery or update the assistant snapshot.

To run the full pipeline in the **already provisioned project**, install the
Google Cloud CLI, sign in with an account authorized to run this workflow, then:

```powershell
gcloud auth login
gcloud workflows run pbmc-qc-workflow --project=pbmc-okf-portfolio --location=europe-west1
```

Inspect the execution in Google Cloud Console ? Workflows ?
`pbmc-qc-workflow`. Cloud runs can incur charges. A different GCP project needs
its own infrastructure, IAM and configuration; the command above is not a fresh
installation. Infrastructure definitions are in [infra/](infra/).

## Future utility and current limits

The next extension is an upload interface: select an `.h5ad` count matrix, choose
QC parameters, submit a run and download its verified results. Supporting other
samples requires input/configuration changes and review of scientific assumptions.
Multi-sample metadata and analysis would be a further extension. These features
are planned, not implemented.

The current input requires sparse, unnormalized counts in `adata.X`, unique cell
barcodes and original gene IDs. This project does not process FASTQ reads, infer
cell types or support disease comparisons. Its full-data checks are designed for
this small dataset; larger inputs need performance and cost evaluation.

The recorded acceptance suite passed **352 Python tests**. Scheduling, recovery
and private failure-alert delivery were verified. Actual cost reporting awaits
billing-export records, so no actual spend or savings figure is claimed.

Detailed working notes in `docs/` are kept locally and ignored by Git.
