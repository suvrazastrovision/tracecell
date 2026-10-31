"""Run pinned ingestion and QC, validating before publishing a local release."""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import anndata as ad
import pandas as pd
from scipy import sparse

from .ingestion import acquire_dataset, canonical_json, read_config, sha256_file
from .qc import compute_qc, validate_policy


OUTPUTS = ("cells.parquet", "genes.parquet", "dataset_summary.parquet", "counts.h5ad")


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def atomic_json(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".json", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        write_json(temporary, value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build_identity(manifest, policy):
    code_dir = Path(__file__).parent
    return {
        "pipeline_version": 1,
        "dataset_manifest": manifest,
        "qc_policy": policy,
        "code_sha256": {
            name: sha256_file(code_dir / name)
            for name in ("ingestion.py", "qc.py", "run_qc.py")
        },
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.system(),
            **{name: version(name) for name in ("anndata", "numpy", "pandas", "scipy", "pyarrow", "PyYAML", "h5py")},
        },
    }


def validate_outputs(directory, adata, frames):
    for name, expected in frames.items():
        actual = pd.read_parquet(directory / name)
        pd.testing.assert_frame_equal(actual, expected)
    cells = frames["cells.parquet"]
    genes = frames["genes.parquet"]
    summary = frames["dataset_summary.parquet"].iloc[0]
    if len(cells) != adata.n_obs or not cells.cell_id.is_unique:
        raise ValueError("Cell identity or cardinality changed during export")
    if len(genes) != adata.n_vars or not genes.gene_id.is_unique:
        raise ValueError("Gene identity or cardinality changed during export")
    if summary.cells_pass + summary.cells_fail != summary.cells_total:
        raise ValueError("QC summary does not reconcile")
    if cells.qc_pass.ne(cells.exclusion_reasons.eq("")).any():
        raise ValueError("QC flags and exclusion reasons disagree")
    restored = ad.read_h5ad(directory / "counts.h5ad")
    if not sparse.issparse(restored.X) or (restored.X != adata.X).nnz:
        raise ValueError("Count matrix changed during export")
    if restored.obs_names.tolist() != cells.cell_id.tolist():
        raise ValueError("AnnData cell identities do not match Parquet")
    if restored.var_names.tolist() != genes.gene_id.tolist():
        raise ValueError("AnnData gene identities do not match Parquet")
    pd.testing.assert_frame_equal(restored.obs, adata.obs)
    pd.testing.assert_frame_equal(restored.var, adata.var)


def validate_release(directory, expected_identity=None):
    metadata = json.loads((directory / "release.json").read_text(encoding="utf-8"))
    if expected_identity is not None and metadata["identity"] != expected_identity:
        raise ValueError("Existing release identity does not match this run")
    for name in OUTPUTS:
        if sha256_file(directory / name) != metadata["outputs"][name]["sha256"]:
            raise ValueError(f"Release checksum mismatch: {name}")
    return metadata


def run_pipeline(manifest_path="configs/dataset.yaml", policy_path="configs/qc.yaml",
                 raw_dir="data/raw", output_dir="data/processed", artifact_dir="artifacts"):
    output_dir, artifact_dir = Path(output_dir), Path(artifact_dir)
    runs_dir = artifact_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    audit = {
        "run_id": uuid.uuid4().hex,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
    }
    try:
        manifest, policy = read_config(manifest_path), read_config(policy_path)
        validate_policy(policy)
        source = acquire_dataset(manifest, raw_dir)
        identity = build_identity(manifest, policy)
        release_id = hashlib.sha256(canonical_json(identity).encode()).hexdigest()
        audit["release_id"] = release_id
        try:
            audit["git_commit"] = subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            audit["git_commit"] = None

        releases_dir = output_dir / "releases"
        releases_dir.mkdir(parents=True, exist_ok=True)
        release_dir = releases_dir / release_id
        reused = release_dir.exists()
        if reused:
            metadata = validate_release(release_dir, identity)
        else:
            adata = ad.read_h5ad(source)
            cells, genes, summary = compute_qc(adata, manifest, policy)
            frames = {"cells.parquet": cells, "genes.parquet": genes, "dataset_summary.parquet": summary}
            # Preserve counts and all cells; replace matrix axes with stable IDs.
            adata.obs = cells.set_index("cell_id")
            adata.var = genes.set_index("gene_id")
            with tempfile.TemporaryDirectory(prefix="staging-", dir=releases_dir) as temp:
                staging = Path(temp) / "release"
                staging.mkdir()
                for name, frame in frames.items():
                    frame.to_parquet(staging / name, index=False, compression="snappy")
                adata.write_h5ad(staging / "counts.h5ad", compression="gzip", convert_strings_to_categoricals=False)
                validate_outputs(staging, adata, frames)
                metadata = {
                    "release_id": release_id,
                    "identity": identity,
                    "summary": summary.iloc[0].to_dict(),
                    "outputs": {name: {"sha256": sha256_file(staging / name)} for name in OUTPUTS},
                }
                write_json(staging / "release.json", metadata)
                validate_release(staging, identity)
                staging.rename(release_dir)
        # A failed validation leaves the previous current pointer unchanged.
        atomic_json(output_dir / "current.json", {"release_id": release_id, "path": f"releases/{release_id}"})
        audit.update(status="success", reused=reused, summary=metadata["summary"])
        return {"release_id": release_id, "release_dir": str(release_dir), "reused": reused, "summary": metadata["summary"]}
    except Exception as error:
        audit.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        audit["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(runs_dir / f"{audit['run_id']}.json", audit)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="configs/dataset.yaml")
    parser.add_argument("--policy", default="configs/qc.yaml")
    parser.add_argument("--raw-dir", default="data/raw")
    parser.add_argument("--output-dir", default="data/processed")
    parser.add_argument("--artifact-dir", default="artifacts")
    args = parser.parse_args()
    try:
        result = run_pipeline(args.manifest, args.policy, args.raw_dir, args.output_dir, args.artifact_dir)
    except Exception as error:
        parser.exit(1, f"QC pipeline failed: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
