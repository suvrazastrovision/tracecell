import json
from io import BytesIO
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
import pytest
import yaml
from scipy import sparse

from pbmc_platform.ingestion import acquire_dataset, sha256_file
from pbmc_platform.qc import compute_qc, validate_policy
from pbmc_platform.run_qc import run_pipeline, validate_release


@pytest.fixture
def dataset():
    # Rows exercise the lower gene boundary, upper gene boundary, exact mito
    # boundary, low genes, zero counts, and overlapping exclusions.
    counts = np.array([
        [0, 1, 1, 0],
        [0, 1, 1, 1],
        [1, 3, 0, 0],
        [0, 1, 0, 0],
        [0, 0, 0, 0],
        [1, 1, 1, 1],
    ], dtype=np.float32)
    return ad.AnnData(
        X=sparse.csr_matrix(counts),
        obs=pd.DataFrame(index=[f"barcode-{n}" for n in range(6)]),
        var=pd.DataFrame(
            {"gene_ids": [f"ENSG{n}" for n in range(4)]},
            index=["MT-A", "B", "C", "D"],
        ),
    )


@pytest.fixture
def policy():
    return {
        "schema_version": 1,
        "policy_id": "fixture",
        "policy_version": "1",
        "min_genes_inclusive": 2,
        "max_genes_exclusive": 3,
        "max_mito_pct_exclusive": 25.0,
        "mitochondrial_prefix": "MT-",
    }


@pytest.fixture
def manifest(dataset):
    return {
        "schema_version": 1,
        "dataset_id": "fixture",
        "dataset_version": "v1",
        "source_url": "https://example.org/counts.h5ad",
        "source_filename": "counts.h5ad",
        "sha256": "0" * 64,
        "expected_cells": dataset.n_obs,
        "expected_features": dataset.n_vars,
        "count_layer": "X",
    }


def test_hand_calculated_metrics_and_threshold_boundaries(dataset, manifest, policy):
    original = dataset.X.copy()
    cells, genes, summary = compute_qc(dataset, manifest, policy)
    assert cells.total_counts.tolist() == [2, 3, 4, 1, 0, 4]
    assert cells.n_genes_by_counts.tolist() == [2, 3, 2, 1, 0, 4]
    assert cells.mitochondrial_counts.tolist() == [0, 0, 1, 0, 0, 1]
    np.testing.assert_allclose(cells.pct_counts_mito, [0, 0, 25, 0, np.nan, 25], equal_nan=True)
    assert cells.qc_pass.tolist() == [True, False, False, False, False, False]
    assert cells.exclusion_reasons.tolist() == [
        "", "high_genes", "high_mito", "low_genes", "zero_counts;low_genes", "high_genes;high_mito",
    ]
    assert cells.cell_id.iloc[0] == "fixture:v1:barcode-0"
    assert genes.gene_id.tolist() == ["ENSG0", "ENSG1", "ENSG2", "ENSG3"]
    assert genes.is_mitochondrial.tolist() == [True, False, False, False]
    assert summary.iloc[0].cells_pass == 1
    assert summary.iloc[0].cells_fail == 5
    assert summary.iloc[0].total_counts == 14
    assert (dataset.X != original).nnz == 0


def test_explicit_sparse_zeros_do_not_count_as_detected_genes(dataset, manifest, policy):
    matrix = dataset.X.tolil()
    matrix.rows[0].append(3)
    matrix.data[0].append(0.0)
    dataset.X = matrix.tocsr()
    cells, _, _ = compute_qc(dataset, manifest, policy)
    assert cells.n_genes_by_counts.iloc[0] == 2


@pytest.mark.parametrize("bad_count", [-1, np.nan, np.inf, 0.5])
def test_rejects_invalid_counts(dataset, manifest, policy, bad_count):
    dataset.X.data[0] = bad_count
    with pytest.raises(ValueError, match="Counts|integer-valued"):
        compute_qc(dataset, manifest, policy)


def test_rejects_missing_mito_annotation(dataset, manifest, policy):
    policy["mitochondrial_prefix"] = "not-present"
    with pytest.raises(ValueError, match="No mitochondrial"):
        compute_qc(dataset, manifest, policy)


def test_rejects_duplicate_gene_ids(dataset, manifest, policy):
    dataset.var["gene_ids"] = ["duplicate"] * dataset.n_vars
    with pytest.raises(ValueError, match="unique"):
        compute_qc(dataset, manifest, policy)


def test_rejects_wrong_dimensions(dataset, manifest, policy):
    manifest["expected_cells"] += 1
    with pytest.raises(ValueError, match="dimensions"):
        compute_qc(dataset, manifest, policy)


@pytest.mark.parametrize("key,value", [
    ("min_genes_inclusive", -1),
    ("max_genes_exclusive", 1),
    ("max_mito_pct_exclusive", 0),
    ("max_mito_pct_exclusive", float("nan")),
    ("max_mito_pct_exclusive", True),
    ("mitochondrial_prefix", ""),
])
def test_rejects_invalid_policy(policy, key, value):
    policy[key] = value
    with pytest.raises(ValueError):
        validate_policy(policy)


def test_failed_download_never_becomes_cached_source(tmp_path, manifest, monkeypatch):
    monkeypatch.setattr("pbmc_platform.ingestion.urlopen", lambda *a, **kw: BytesIO(b"corrupt download"))
    with pytest.raises(ValueError, match="pinned SHA-256"):
        acquire_dataset(manifest, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_verified_download_is_reused_without_network(tmp_path, manifest, monkeypatch):
    import hashlib

    payload = b"checksum fixture"
    manifest["sha256"] = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr("pbmc_platform.ingestion.urlopen", lambda *a, **kw: BytesIO(payload))
    path = acquire_dataset(manifest, tmp_path)
    assert path.read_bytes() == payload

    def unexpected_download(*args, **kwargs):
        pytest.fail("A valid cached file must not trigger a download")

    monkeypatch.setattr("pbmc_platform.ingestion.urlopen", unexpected_download)
    assert acquire_dataset(manifest, tmp_path) == path


@pytest.fixture
def pipeline_setup(tmp_path, dataset, manifest, policy):
    raw = tmp_path / "raw"
    raw.mkdir()
    source = raw / manifest["source_filename"]
    dataset.write_h5ad(source)
    manifest["sha256"] = sha256_file(source)
    manifest_path, policy_path = tmp_path / "dataset.yaml", tmp_path / "qc.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    policy_path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    return dict(
        manifest_path=manifest_path,
        policy_path=policy_path,
        raw_dir=raw,
        output_dir=tmp_path / "processed",
        artifact_dir=tmp_path / "artifacts",
    )


def test_pipeline_roundtrip_and_idempotent_replay(pipeline_setup, dataset):
    result = run_pipeline(**pipeline_setup)
    directory = pipeline_setup["output_dir"] / "releases" / result["release_id"]
    initial_files = {file.name: file.read_bytes() for file in directory.iterdir()}
    replay = run_pipeline(**pipeline_setup)
    assert not result["reused"]
    assert replay["reused"]
    assert result["release_id"] == replay["release_id"]
    assert initial_files == {file.name: file.read_bytes() for file in directory.iterdir()}
    cells = pd.read_parquet(directory / "cells.parquet")
    assert len(cells) == dataset.n_obs
    assert cells.pct_counts_mito.isna().sum() == 1
    restored = ad.read_h5ad(directory / "counts.h5ad")
    assert (restored.X != dataset.X).nnz == 0
    assert restored.obs_names.tolist() == cells.cell_id.tolist()
    assert len(list((pipeline_setup["artifact_dir"] / "runs").glob("*.json"))) == 2


def test_failed_input_keeps_previous_release_and_records_failure(pipeline_setup):
    run_pipeline(**pipeline_setup)
    pointer = pipeline_setup["output_dir"] / "current.json"
    before = pointer.read_bytes()
    source = pipeline_setup["raw_dir"] / "counts.h5ad"
    source.write_bytes(b"corrupt cached input")
    with pytest.raises(ValueError, match="Checksum mismatch"):
        run_pipeline(**pipeline_setup)
    assert pointer.read_bytes() == before
    audits = [json.loads(p.read_text()) for p in (pipeline_setup["artifact_dir"] / "runs").glob("*.json")]
    assert sorted(audit["status"] for audit in audits) == ["failed", "success"]


def test_bad_policy_output_never_replaces_previous_release(pipeline_setup):
    run_pipeline(**pipeline_setup)
    pointer = pipeline_setup["output_dir"] / "current.json"
    before = pointer.read_bytes()
    policy = yaml.safe_load(pipeline_setup["policy_path"].read_text())
    policy.update(policy_version="2", mitochondrial_prefix="not-present")
    pipeline_setup["policy_path"].write_text(yaml.safe_dump(policy), encoding="utf-8")
    with pytest.raises(ValueError, match="No mitochondrial"):
        run_pipeline(**pipeline_setup)
    assert pointer.read_bytes() == before
    assert len(list((pipeline_setup["output_dir"] / "releases").iterdir())) == 1


def test_export_validation_failure_cleans_staging_and_keeps_pointer(pipeline_setup, monkeypatch):
    run_pipeline(**pipeline_setup)
    pointer = pipeline_setup["output_dir"] / "current.json"
    before = pointer.read_bytes()
    policy = yaml.safe_load(pipeline_setup["policy_path"].read_text())
    policy["policy_version"] = "2"
    pipeline_setup["policy_path"].write_text(yaml.safe_dump(policy), encoding="utf-8")

    def broken_export(*args):
        raise ValueError("Injected export validation failure")

    monkeypatch.setattr("pbmc_platform.run_qc.validate_outputs", broken_export)
    with pytest.raises(ValueError, match="Injected export"):
        run_pipeline(**pipeline_setup)
    assert pointer.read_bytes() == before
    assert len(list((pipeline_setup["output_dir"] / "releases").iterdir())) == 1


def test_policy_change_publishes_separate_release_with_stable_cell_ids(pipeline_setup):
    first = run_pipeline(**pipeline_setup)
    policy = yaml.safe_load(pipeline_setup["policy_path"].read_text())
    policy.update(policy_version="2", max_mito_pct_exclusive=30)
    pipeline_setup["policy_path"].write_text(yaml.safe_dump(policy), encoding="utf-8")
    second = run_pipeline(**pipeline_setup)
    assert first["release_id"] != second["release_id"]
    assert first["summary"]["cells_pass"] == 1
    assert second["summary"]["cells_pass"] == 2
    first_cells = pd.read_parquet(Path(first["release_dir"]) / "cells.parquet")
    second_cells = pd.read_parquet(Path(second["release_dir"]) / "cells.parquet")
    assert first_cells.cell_id.tolist() == second_cells.cell_id.tolist()


def test_tampered_release_is_rejected(pipeline_setup):
    result = run_pipeline(**pipeline_setup)
    directory = pipeline_setup["output_dir"] / "releases" / result["release_id"]
    (directory / "cells.parquet").write_bytes(b"corrupt output")
    with pytest.raises(ValueError, match="Release checksum mismatch"):
        validate_release(directory)
    with pytest.raises(ValueError, match="Release checksum mismatch"):
        run_pipeline(**pipeline_setup)
