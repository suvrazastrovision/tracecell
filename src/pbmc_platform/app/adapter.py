"""Inspect bounded HDF5 before reading a selected sparse count matrix."""
from pathlib import Path

import anndata as ad
import h5py
import numpy as np
import pandas as pd
from scipy import sparse

from .contracts import MAX_BYTES, MAX_CELLS, MAX_GENES, MAX_NNZ


def inspect(path):
    path = Path(path)
    if not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError("Upload exceeds the 250 MiB limit or is empty")
    with h5py.File(path, "r") as f:
        # Reject links to external HDF5 content and decoded payload bombs before AnnData.
        total = 0
        seen = set()
        def visit(group):
            nonlocal total
            address = h5py.h5o.get_info(group.id).addr
            if address in seen:
                raise ValueError("Shared/cyclic HDF5 groups are unsupported")
            seen.add(address)
            for key in group:
                if not isinstance(group.get(key, getlink=True), h5py.HardLink):
                    raise ValueError("External and soft HDF5 links are unsupported")
                item = group[key]
                if isinstance(item, h5py.Group):
                    visit(item)
                else:
                    if item.is_virtual or item.external:
                        raise ValueError("Virtual/external HDF5 datasets are unsupported")
                    total += item.size * max(item.dtype.itemsize, 8)
                    if total > 512 * 1024 * 1024:
                        raise ValueError("Decoded HDF5 payload exceeds inspection limit")
        visit(f)
        def matrix_info(node):
            if not isinstance(node, h5py.Group):
                return None
            encoding = node.attrs.get("encoding-type")
            if not encoding and node.attrs.get("h5sparse_format") in ("csr", "csc"):
                encoding = node.attrs["h5sparse_format"] + "_matrix"
            if encoding not in ("csr_matrix", "csc_matrix"):
                return None
            shape = tuple(int(x) for x in (node.attrs["shape"] if "shape" in node.attrs else node.attrs["h5sparse_shape"]))
            if len(shape) != 2 or not (0 < shape[0] <= MAX_CELLS and 0 < shape[1] <= MAX_GENES):
                raise ValueError("Supported dimensions: 1-20,000 cells and 1-40,000 features")
            if node["data"].size > MAX_NNZ:
                raise ValueError("Matrix exceeds 10 million stored entries")
            return {"cells": shape[0], "features": shape[1], "entries": int(node["data"].size)}
        matrices = {}
        if "X" in f and (info := matrix_info(f["X"])):
            matrices["X"] = info
        if "layers" in f:
            for key in f["layers"]:
                if info := matrix_info(f["layers"][key]):
                    matrices["layer:" + key] = info
        if not matrices:
            raise ValueError("No supported sparse matrix in X or a named layer")
        var = f["var"]
        names = [str(x) for x in var.attrs.get("column-order", [])]
        if isinstance(var, h5py.Dataset):
            names = [str(x) for x in (var.dtype.names or ()) if x not in ("index", "_index")]
        return {"matrices": matrices, "gene_columns": ["index", *names],
                "limits": {"bytes": MAX_BYTES, "cells": MAX_CELLS, "genes": MAX_GENES, "nnz": MAX_NNZ}}


def selected(path, config):
    info = inspect(path)
    if config.counts not in info["matrices"]:
        raise ValueError("Select one of the inspected sparse count matrices")
    # Read only the selected matrix and metadata, not every unselected layer/embedding.
    from anndata.io import read_elem
    with h5py.File(path, "r") as f:
        if isinstance(f["var"], h5py.Dataset):
            legacy = ad.read_h5ad(path)
            x, obs, var = legacy.X, legacy.obs, legacy.var
        else:
            x = read_elem(f["X"] if config.counts == "X" else f["layers"][config.counts.removeprefix("layer:")])
            obs, var = read_elem(f["obs"]), read_elem(f["var"])
    if not sparse.issparse(x):
        raise ValueError("Sparse counts required")
    x.check_format(full_check=True)
    if x.data.size and (np.max(x.data) > 2**53 - 1 or np.sum(x.data, dtype=np.float64) > 2**53 - 1):
        raise ValueError("Count values/totals exceed the exact integer precision supported by this pilot")
    x = x.astype(np.float64).tocsr()
    x.sum_duplicates()
    x.eliminate_zeros()
    if not np.isfinite(x.data).all() or (x.data < 0).any() or (x.data != np.floor(x.data)).any():
        raise ValueError("Select original, finite, nonnegative integer counts; normalized values are unsupported")
    if not obs.index.is_unique or any(not str(v).strip() for v in obs.index):
        raise ValueError("Cell barcodes must be unique and nonempty")
    def column(name):
        values = pd.Series(var.index.to_numpy()) if name == "index" else var[name].reset_index(drop=True)
        if values.isna().any() or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError("Gene identifiers and symbols must be nonempty strings")
        return values.to_numpy()
    ids, symbols = column(config.gene_ids), column(config.gene_symbols)
    if len(set(ids)) != len(ids):
        raise ValueError("Original gene identifiers must be unique")
    if config.mito_mode == "column":
        values = var[config.mito_value]
        if values.isna().any() or not pd.api.types.is_bool_dtype(values.dtype):
            raise ValueError("Mitochondrial annotation must be a non-null boolean column")
        mito = values.to_numpy(dtype=bool)
    else:
        mito = np.array([v.startswith(config.mito_value) for v in symbols])
    if not mito.any():
        raise ValueError("No mitochondrial features match the selected annotation")
    result = ad.AnnData(x, obs=pd.DataFrame(index=obs.index.copy()),
                        var=pd.DataFrame({"gene_ids": ids, "source_symbol": symbols, "app_mito": mito}, index=var.index.copy()))
    # Adapt the existing QC implementation through a synthetic, unambiguous prefix.
    result.var_names = [("APP-MT-" if flag else "APP-GENE-") + str(i) for i, flag in enumerate(mito)]
    return result


def qc_frames(path, config, run_id):
    from ..qc import compute_qc
    matrix = selected(path, config)
    manifest = {"dataset_id": "sample_" + run_id, "dataset_version": "1",
                "expected_cells": matrix.n_obs, "expected_features": matrix.n_vars}
    policy = {"policy_id": "app", "policy_version": "2", "min_genes_inclusive": config.min_genes,
              "max_genes_exclusive": config.max_genes, "max_mito_pct_exclusive": config.max_mito_pct,
              "mitochondrial_prefix": "APP-MT-"}
    cells, genes, summary = compute_qc(matrix, manifest, policy)
    genes["source_feature_name"] = matrix.var["source_symbol"].to_numpy()
    return matrix, cells, genes, summary
