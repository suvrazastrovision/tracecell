# PBMC Knowledge Platform

## Intended user

A scientist exploring public PBMC data and its quality constraints.

## First release

Ingest unnormalized counts, calculate per-cell QC, publish analytics,
and generate versioned OKF context for a scientist-facing assistant.

## Scientific boundary

The initial dataset contains one healthy donor.
The platform does not infer cell types or support disease comparisons.

## Engineering goals

Reproducible inputs, traceable transformations, idempotent publication,
tested recovery, and measurable cloud cost.

## Initial dataset

Use the unnormalized PBMC3k AnnData copy provided by Scanpy for initial
inspection. Its input is the filtered gene-barcode count matrix, not FASTQ
reads and not a normalized expression matrix.

- Original source: [10x Genomics PBMC3k](https://www.10xgenomics.com/datasets/3-k-pbm-cs-from-a-healthy-donor-1-standard-1-1-0).
- Loader: [scanpy.datasets.pbmc3k](https://scanpy.readthedocs.io/en/stable/generated/scanpy.datasets.pbmc3k.html).
- Attribution: 10x Genomics; dataset licensed under CC BY 4.0.
- Expected dimensions: 2,700 cells and 32,738 features.

The initial inspection records the acquired file's SHA-256 checksum.
The QC pipeline verifies that pin against `configs/dataset.yaml` before
processing; see [QC contracts and recovery](002-qc-pipeline.md).
