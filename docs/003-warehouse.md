# Warehouse ingestion and analytics

## Current capability

The loader validates a checkpoint 2 release, loads its three Parquet tables
into BigQuery, and reads rows back to verify that values match the local
files. It also supports DuckDB for local SQL verification without credentials.
The six dbt models run against either adapter. Live BigQuery execution still
requires a real GCP project ID, permissions, and Application Default Credentials.

The sparse expression matrix stays outside the warehouse. BigQuery receives
cell metadata/QC, gene metadata, and dataset summaries. Raw FASTQ processing,
cell-type inference, and biological differential expression remain outside v1.

## Installation

Run every command from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-warehouse-dev.txt
.\.venv\Scripts\python.exe -m pip check
$env:PYTHONPATH = "src"
$env:PBMC_RELEASE_ID = (Get-Content data/processed/current.json | ConvertFrom-Json).release_id
```

For cloud use without the local DuckDB adapter, install
`requirements-warehouse.txt` instead of `requirements-warehouse-dev.txt`.
The combined `requirements-warehouse-lock.txt` records the tested Windows
environment; install it by itself to reproduce that complete environment.
Do not combine it with the older checkpoint 1 lock in the same pip command.

## Offline validation

```powershell
.\.venv\Scripts\python.exe -m pbmc_platform.load_warehouse --dry-run --release-id $env:PBMC_RELEASE_ID
.\.venv\Scripts\python.exe -m pbmc_platform.load_warehouse --target local --release-id $env:PBMC_RELEASE_ID
.\.venv\Scripts\dbt.exe build --project-dir warehouse/dbt --profiles-dir warehouse/dbt --target local
.\.venv\Scripts\python.exe -m pytest -q
```

The dry run validates local hashes, release identity, schemas, keys, and
summary reconciliation. It does not authenticate, inspect cloud resources,
or estimate BigQuery query costs. It writes a local audit with status `dry_run`.

DuckDB defaults to `artifacts/pbmc.duckdb`. Override it with the same
`PBMC_DUCKDB_PATH` environment variable for both loader and dbt.
Local results establish SQL/data correctness; they do not validate Google
Cloud permissions or the live BigQuery service.

## Connect to BigQuery

Create or select a GCP project with billing enabled, then install the
[Google Cloud CLI](https://docs.cloud.google.com/sdk/docs/install).
Replace `YOUR_PROJECT_ID` below with your actual project ID:

```powershell
$env:GOOGLE_CLOUD_PROJECT = "YOUR_PROJECT_ID"
$env:PBMC_BQ_LOCATION = "europe-west1"
$env:PBMC_RAW_DATASET = "pbmc_raw"
$env:PBMC_ANALYTICS_DATASET = "pbmc_analytics"

gcloud auth login
gcloud config set project $env:GOOGLE_CLOUD_PROJECT
gcloud services enable bigquery.googleapis.com
gcloud auth application-default login
gcloud auth application-default set-quota-project $env:GOOGLE_CLOUD_PROJECT
```

The Python client and dbt OAuth profile both use ADC. No service-account key
file is required in this repository. See Google's
[BigQuery authentication guide](https://docs.cloud.google.com/bigquery/docs/authentication).

The identity must be able to create datasets, run jobs, and read/write tables.
For pre-created datasets, grant BigQuery Job User at the project and BigQuery
Data Editor on the relevant raw/analytics datasets. Dataset creation needs
additional project-level `bigquery.datasets.create` permission; enabling the
API also needs service-usage permissions. Use your own portfolio project or
ask its administrator to grant the specific access required. See the
[dbt BigQuery setup documentation](https://docs.getdbt.com/docs/local/connect-data-platform/bigquery-setup?version=1.12).

Upload the pinned release, then build its analytics:

```powershell
$env:PBMC_RELEASE_ID = (Get-Content data/processed/current.json | ConvertFrom-Json).release_id
.\.venv\Scripts\python.exe -m pbmc_platform.load_warehouse --release-id $env:PBMC_RELEASE_ID
.\.venv\Scripts\dbt.exe debug --project-dir warehouse/dbt --profiles-dir warehouse/dbt --target bigquery
.\.venv\Scripts\dbt.exe build --project-dir warehouse/dbt --profiles-dir warehouse/dbt --target bigquery
```

Run the commands separately and stop if any command fails. Keep the same
release ID and dataset/project environment variables for both loader and dbt.
The default dbt target is BigQuery, so include `--target local` for offline work.

## Table and model contracts

The raw dataset contains `cells_r_<release_id>`, `genes_r_<release_id>`, and
`dataset_summary_r_<release_id>`. Table descriptions record the full release ID
and Parquet checksum. Schemas are derived from validated Parquet primitive
types. Empty tables may be populated with `WRITE_EMPTY`; nonempty tables must
already match the expected contract and rows. Existing data is never appended
to or truncated by the loader.

The dbt dataset is `pbmc_analytics_r_<release_id>`. All staging rows carry a
`release_id` column. Models are isolated per release:

| Model | Purpose |
| --- | --- |
| `stg_cells` | All cells, identities, metrics, policy versions, flags, and reasons |
| `stg_genes` | Original Ensembl IDs and mitochondrial/source annotations |
| `stg_dataset_summary` | Producer's original summary for comparison |
| `mart_dataset_qc` | Recomputed QC counts, feature counts, means, and pass fraction |
| `mart_qc_exclusions` | Four exclusion-reason counts; counts may overlap |
| `mart_cohort_cells` | All cells passing the recorded policy |

Means in `mart_dataset_qc` describe all input cells, including those that fail
QC. Undefined mitochondrial percentages for zero-count cells are omitted
from that mean. The cohort is QC-based; it does not imply biological labels
or independent donors. Each release supports one dataset/policy combination.

For the pinned PBMC3k release, acceptance is 2,700 total cells, 2,638 passing,
62 failing, 32,738 features, and 6,390,631 total counts. Exclusion counts are
5 high-gene cells and 57 high-mitochondrial cells.

Example after a successful cloud build (substitute project and release ID):

```sql
SELECT cells_total, cells_pass, cells_fail, features_total, qc_pass_fraction
FROM `YOUR_PROJECT_ID.pbmc_analytics_r_RELEASE_ID.mart_dataset_qc`;
```

## Retry, validation, and cost controls

Repeat the same loader command after a partial failure. Completed tables are
verified and reused; empty tables resume loading. Failed load attempts get
new job IDs, and `WRITE_EMPTY` prevents a repeated attempt from appending
duplicates. Processing is intended for a single writer. Conflicting external
writers can cause a safe failure that requires a retry.

Every real load verifies complete remote table contents, including on replay.
This deliberately favors strong verification for a small demonstration dataset.
For larger datasets, replace full readback with scalable content validation;
do not claim this implementation has been benchmarked at that scale.

Run audits live in `artifacts/warehouse_runs/` and include target, release,
status, timestamps, and successful load job IDs. A failed run may have created
some raw tables; it never creates analytics models or changes other releases.
Preserve suspect tables for investigation rather than overwriting them.

`dbt build` includes key/null checks, metric invariants, producer-to-warehouse
reconciliation, and cohort reconciliation. Consume a release's analytics only
after the build exits successfully. A failed build can leave partial models
inside that release's own analytics dataset; other release datasets remain
separate. No cross-release "current" cloud pointer is published in this checkpoint.

The profile limits each BigQuery query to 100,000,000 billed bytes, two threads,
300 seconds, and one retry. This is a per-query limit, not a monthly spending
cap. Raw tables are intentionally unpartitioned at this dataset size. Cloud
Storage export, automated retention, Terraform, and scheduling follow in
checkpoint 4. Configure your project budget separately.

## Verification boundary

Python tests use real BigQuery schema/load configuration objects with a
simulated service to exercise no-duplicate retries, partial failure, location
mismatch, source-contract conflicts, and modified data. DuckDB executes the
actual dbt models and data tests against the PBMC release. Live cloud behavior
and billing must be checked after supplying the project ID and credentials.
