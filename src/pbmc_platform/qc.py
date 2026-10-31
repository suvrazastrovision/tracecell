"""Sparse count QC with explicit cell retention and stable identifiers."""

import numpy as np
import pandas as pd
from scipy import sparse


def validate_policy(policy):
    for key in ("policy_id", "policy_version", "mitochondrial_prefix"):
        if not isinstance(policy.get(key), str) or not policy[key].strip():
            raise ValueError(f"{key} must be a nonempty string")
    for key in ("min_genes_inclusive", "max_genes_exclusive"):
        if type(policy.get(key)) is not int or policy[key] < 0:
            raise ValueError(f"{key} must be a nonnegative integer")
    if policy["min_genes_inclusive"] >= policy["max_genes_exclusive"]:
        raise ValueError("The minimum gene threshold must be below the maximum")
    maximum = policy.get("max_mito_pct_exclusive")
    if isinstance(maximum, bool) or not isinstance(maximum, (int, float)):
        raise ValueError("Mitochondrial threshold must be numeric")
    if not np.isfinite(maximum) or not 0 < maximum <= 100:
        raise ValueError("Mitochondrial threshold must be in (0, 100]")


def validate_counts(adata, manifest):
    if adata.shape != (manifest["expected_cells"], manifest["expected_features"]):
        raise ValueError("Input dimensions do not match the dataset manifest")
    if not sparse.issparse(adata.X):
        raise ValueError("The count matrix must be sparse")
    if not adata.obs_names.is_unique or any(not str(x).strip() for x in adata.obs_names):
        raise ValueError("Cell barcodes must be nonempty and unique")
    if "gene_ids" not in adata.var:
        raise ValueError("Input is missing original gene_ids")
    gene_ids = adata.var["gene_ids"]
    if gene_ids.isna().any() or not gene_ids.is_unique:
        raise ValueError("Original gene IDs must be present and unique")
    if any(not isinstance(x, str) or not x.strip() for x in gene_ids):
        raise ValueError("Gene IDs must be nonempty strings")
    values = adata.X.data
    if not np.isfinite(values).all() or (values < 0).any():
        raise ValueError("Counts must be finite and nonnegative")
    if (values != np.floor(values)).any():
        raise ValueError("Expected unnormalized integer-valued counts")


def compute_qc(adata, manifest, policy):
    validate_policy(policy)
    validate_counts(adata, manifest)
    # Float64 accumulation prevents loss of precision from float32 count storage.
    matrix = adata.X.astype(np.float64).tocsr(copy=True)
    matrix.sum_duplicates()
    matrix.eliminate_zeros()
    mito = np.asarray(adata.var_names.str.startswith(policy["mitochondrial_prefix"]))
    if not mito.any():
        raise ValueError("No mitochondrial features match the configured prefix")

    totals = np.asarray(matrix.sum(axis=1)).ravel()
    if not np.isfinite(totals).all() or (totals >= 2**63).any():
        raise ValueError("Cell count totals exceed supported int64 range")
    detected = matrix.getnnz(axis=1).astype(np.int64)
    mito_counts = np.asarray(matrix[:, mito].sum(axis=1)).ravel()
    mito_pct = np.divide(
        mito_counts * 100.0, totals,
        out=np.full(adata.n_obs, np.nan), where=totals > 0,
    )

    flags = {
        "zero_counts": totals == 0,
        "low_genes": detected < policy["min_genes_inclusive"],
        "high_genes": detected >= policy["max_genes_exclusive"],
        "high_mito": mito_pct >= policy["max_mito_pct_exclusive"],
    }
    reasons = [
        ";".join(name for name, values in flags.items() if values[row])
        for row in range(adata.n_obs)
    ]
    prefix = f"{manifest['dataset_id']}:{manifest['dataset_version']}:"
    cells = pd.DataFrame({
        "cell_id": [prefix + barcode for barcode in adata.obs_names],
        "dataset_id": manifest["dataset_id"],
        "dataset_version": manifest["dataset_version"],
        "barcode": adata.obs_names.tolist(),
        "total_counts": totals.astype(np.int64),
        "n_genes_by_counts": detected,
        "mitochondrial_counts": mito_counts.astype(np.int64),
        "pct_counts_mito": mito_pct,
        **flags,
        "qc_pass": [not reason for reason in reasons],
        "exclusion_reasons": reasons,
        "policy_id": policy["policy_id"],
        "policy_version": policy["policy_version"],
    })
    genes = pd.DataFrame({
        "dataset_id": manifest["dataset_id"],
        "dataset_version": manifest["dataset_version"],
        "gene_id": adata.var["gene_ids"].tolist(),
        "source_feature_name": adata.var_names.tolist(),
        "is_mitochondrial": mito,
    })
    summary = {
        "dataset_id": manifest["dataset_id"],
        "dataset_version": manifest["dataset_version"],
        "policy_id": policy["policy_id"],
        "policy_version": policy["policy_version"],
        "cells_total": adata.n_obs,
        "features_total": adata.n_vars,
        "cells_pass": int(cells.qc_pass.sum()),
        "cells_fail": int((~cells.qc_pass).sum()),
        "total_counts": int(cells.total_counts.sum()),
        **{f"cells_{name}": int(values.sum()) for name, values in flags.items()},
    }
    return cells, genes, pd.DataFrame([summary])
