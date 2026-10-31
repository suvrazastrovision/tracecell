# PBMC Knowledge Platform

A GCP data engineering portfolio project for reproducible scRNA-seq data
processing, quality controls, analytics, and Open Knowledge Format (OKF)
context for scientists.

Checkpoints 1 and 2 implement local dataset inspection, pinned ingestion,
and validated QC releases. Checkpoint 3 adds a BigQuery loader and dbt
analytics models with a local DuckDB verification target. Cloud execution
requires your GCP project and credentials. Later checkpoints add cloud
orchestration, OKF publishing, and a cited assistant.
See [project purpose](docs/001-project-purpose.md) for the scientific boundaries.

## Local setup (Windows PowerShell)

Run commands from the repository root with Python 3.11:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
```

Reuse an existing `.venv` by skipping its creation. No virtual environment
activation, cloud account, or API key is needed for this checkpoint.

## Inspect the dataset

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m pbmc_platform.inspect_dataset
```

The first run downloads the PBMC3k dataset. Later runs reuse the local file.
The report is printed and saved to `artifacts/dataset_inspection.json`.
Expected dimensions are **2,700 cells × 32,738 features**, stored as a sparse
matrix. The report includes barcode uniqueness, metadata columns, and a
SHA-256 checksum of `data/raw/pbmc3k_raw.h5ad`.

## Working directories

| Directory | Purpose |
| --- | --- |
| `src/pbmc_platform` | Reusable pipeline code |
| `configs` | Dataset manifests and QC policies |
| `tests` | Correctness and recovery tests |
| `data` | Local input and processed datasets (ignored by Git) |
| `artifacts` | Local reports and run evidence (ignored by Git) |
| `knowledge` | Published OKF documents |
| `docs` | Architecture decisions and runbooks |

## Reproduce the verified Windows environment

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
```

`requirements-lock.txt` records the resolved Windows development environment.
The future Linux container will be resolved and verified separately.

## Run the QC pipeline

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m pbmc_platform.run_qc
```

The pipeline checks the SHA-256 declared in `configs/dataset.yaml` before
reading the cached file. If the file is absent, it downloads that exact source
and verifies it before caching. A mismatched cached file fails without
overwriting it. The QC policy lives in `configs/qc.yaml`.

The default demonstration policy retains cells with at least 200 detected
genes, fewer than 2,500 detected genes, and less than 5% mitochondrial counts.
Every input cell remains in the output, including excluded cells with explicit
flags and reasons. These thresholds are adjustable examples, not universal
scientific recommendations.

`data/processed/current.json` points to a validated release under
`data/processed/releases/<release_id>/` containing:

| File | Contents |
| --- | --- |
| `cells.parquet` | All cells, stable IDs, QC metrics, flags, and exclusion reasons |
| `genes.parquet` | Original Ensembl IDs, source feature names, and mitochondrial flags |
| `dataset_summary.parquet` | One row reconciling retained/excluded cells and counts |
| `counts.h5ad` | Unmodified sparse counts with stable cell/gene IDs and QC annotations |
| `release.json` | Input/policy/code/runtime identity and output checksums |

Each invocation writes a separate audit record to `artifacts/runs/`.
Identical inputs, policy, pipeline code, and runtime reuse the same release
after verifying output checksums. Failed runs leave the previous current
pointer untouched. This local publisher assumes one writer at a time.

To select different configuration files or directories:

```powershell
.\.venv\Scripts\python.exe -m pbmc_platform.run_qc --help
```

See [QC contracts and recovery](docs/002-qc-pipeline.md) for definitions,
versioning rules, and recovery instructions.

## Run tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use a tiny generated sparse count matrix; they do not download data
or require cloud credentials.

## Warehouse analytics (checkpoint 3)

Install warehouse dependencies in the existing virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-warehouse-dev.txt
$env:PYTHONPATH = "src"
$env:PBMC_RELEASE_ID = (Get-Content data/processed/current.json | ConvertFrom-Json).release_id
```

Preview the verified load plan without cloud access:

```powershell
.\.venv\Scripts\python.exe -m pbmc_platform.load_warehouse --dry-run
```

Build and test the same analytics models locally:

```powershell
.\.venv\Scripts\python.exe -m pbmc_platform.load_warehouse --target local --release-id $env:PBMC_RELEASE_ID
.\.venv\Scripts\dbt.exe build --project-dir warehouse/dbt --profiles-dir warehouse/dbt --target local
```

Raw tables are immutable per release. dbt builds into
`pbmc_analytics_r_<release_id>` so separate releases cannot mix.
The marts provide dataset QC, exclusion counts, and QC-passing cohort cells.

For BigQuery authentication, exact cloud commands, IAM requirements, and
recovery steps, follow [the warehouse guide](docs/003-warehouse.md).
`requirements-warehouse-lock.txt` snapshots the combined verified Windows
environment, including the local test adapter.

## Next checkpoint

Connect the checkpoint 3 loader to your GCP project and run `dbt build` with
the BigQuery target. After live cloud verification, checkpoint 4 adds
Terraform, containers, and pipeline orchestration.
