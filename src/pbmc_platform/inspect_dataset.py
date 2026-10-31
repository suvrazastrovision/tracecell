"""Download the Scanpy PBMC3k count dataset and report its structure."""

import hashlib
import json
from pathlib import Path

import scanpy as sc
from scipy import sparse


def main():
    raw_dir = Path("data/raw")
    artifact_dir = Path("artifacts")
    raw_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    sc.settings.datasetdir = raw_dir
    adata = sc.datasets.pbmc3k()

    dataset_path = raw_dir / "pbmc3k_raw.h5ad"
    sha256 = hashlib.sha256()
    with dataset_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha256.update(chunk)

    report = {
        "dataset_id": "10x_pbmc3k",
        "acquisition_method": "scanpy.datasets.pbmc3k",
        "cells": adata.n_obs,
        "features": adata.n_vars,
        "sparse_matrix": bool(sparse.issparse(adata.X)),
        "matrix_dtype": str(adata.X.dtype),
        "unique_cell_barcodes": bool(adata.obs_names.is_unique),
        "unique_feature_names": bool(adata.var_names.is_unique),
        "obs_columns": adata.obs.columns.tolist(),
        "var_columns": adata.var.columns.tolist(),
        "source_file": dataset_path.as_posix(),
        "sha256": sha256.hexdigest(),
    }

    output = json.dumps(report, indent=2)
    (artifact_dir / "dataset_inspection.json").write_text(
        output + "\n", encoding="utf-8"
    )
    print(output)


if __name__ == "__main__":
    main()
