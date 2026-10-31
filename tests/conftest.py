"""Small generated release shared by warehouse tests; no network access."""

import anndata as ad
import numpy as np
import pandas as pd
import pytest
import yaml
from scipy import sparse

from pbmc_platform.ingestion import sha256_file
from pbmc_platform.load_warehouse import prepare_load
from pbmc_platform.run_qc import run_pipeline


@pytest.fixture
def warehouse_release(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    source = raw / "fixture.h5ad"
    ad.AnnData(
        X=sparse.csr_matrix(np.array([[0, 200], [5, 95], [0, 0]], dtype=np.float32)),
        obs=pd.DataFrame(index=["pass", "mito-boundary", "zero"]),
        var=pd.DataFrame({"gene_ids": ["ENSG1", "ENSG2"]}, index=["MT-A", "GENE-B"]),
    ).write_h5ad(source)
    manifest = {
        "schema_version": 1, "dataset_id": "warehouse_fixture", "dataset_version": "v1",
        "source_url": "https://example.org/fixture.h5ad", "source_filename": source.name,
        "sha256": sha256_file(source), "expected_cells": 3, "expected_features": 2,
        "count_layer": "X",
    }
    policy = {
        "schema_version": 1, "policy_id": "fixture", "policy_version": "1",
        "min_genes_inclusive": 1, "max_genes_exclusive": 3,
        "max_mito_pct_exclusive": 5.0, "mitochondrial_prefix": "MT-",
    }
    manifest_path, policy_path = tmp_path / "dataset.yaml", tmp_path / "qc.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    policy_path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    processed = tmp_path / "processed"
    run_pipeline(manifest_path, policy_path, raw, processed, tmp_path / "artifacts")
    return prepare_load(processed), processed
