"""Independently validate a draft against pinned release evidence and current warehouse rows."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
import uuid

import anndata as ad
import numpy as np
import pandas as pd
from scipy import sparse
import yaml

from .cloud_release import download_release, download_verified
from .generate_knowledge import LIMITATIONS, REASONS, SCHEMA_PATH, render_report, validate_structure
from .ingestion import sha256_file, validate_manifest
from .load_warehouse import compare_rows, prepare_load, table_description, validate_identifier
from .publish_gcs import validate_destination
from .qc import validate_policy
from .run_cloud_dbt import summarize_results
from .run_qc import OUTPUTS


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError(f"Non-finite JSON number: {value}")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)


def require_equal(actual, expected, label):
    if actual != expected:
        raise ValueError(f"{label} differs from verified source")


def recompute_release(directory, metadata):
    """Recompute measured values and policy flags without calling the QC producer."""
    manifest, policy = (metadata["identity"][key] for key in ("dataset_manifest", "qc_policy"))
    validate_manifest(manifest)
    validate_policy(policy)
    if manifest["donor_count"] != 1:
        raise ValueError("Only the one-donor project release is supported")
    cells = pd.read_parquet(directory / "cells.parquet")
    genes = pd.read_parquet(directory / "genes.parquet")
    matrix_data = ad.read_h5ad(directory / "counts.h5ad")
    if (not sparse.issparse(matrix_data.X)
            or matrix_data.shape != (manifest["expected_cells"], manifest["expected_features"])):
        raise ValueError("Count matrix shape or sparse representation differs")
    require_equal(matrix_data.obs_names.tolist(), cells.cell_id.tolist(), "Matrix cell axis")
    require_equal(matrix_data.var_names.tolist(), genes.gene_id.tolist(), "Matrix gene axis")
    matrix = matrix_data.X.astype(np.float64).tocsr(copy=True)
    matrix.sum_duplicates()
    matrix.eliminate_zeros()
    if (not np.isfinite(matrix.data).all() or (matrix.data < 0).any()
            or (matrix.data != np.floor(matrix.data)).any()):
        raise ValueError("Count matrix must contain finite nonnegative integer counts")
    mito = genes.source_feature_name.str.startswith(policy["mitochondrial_prefix"]).to_numpy()
    if not mito.any() or not np.array_equal(mito, genes.is_mitochondrial.to_numpy()):
        raise ValueError("Mitochondrial feature labels differ from the recorded prefix")
    totals = np.asarray(matrix.sum(axis=1)).ravel()
    detected = matrix.getnnz(axis=1)
    mito_counts = np.asarray(matrix[:, mito].sum(axis=1)).ravel()
    percentages = np.full(len(cells), np.nan)
    np.divide(mito_counts * 100, totals, out=percentages, where=totals > 0)
    measured = {"total_counts": totals, "n_genes_by_counts": detected,
                "mitochondrial_counts": mito_counts, "pct_counts_mito": percentages}
    for name, expected in measured.items():
        if not np.allclose(cells[name].to_numpy(), expected, rtol=0, atol=1e-12, equal_nan=True):
            raise ValueError(f"Cell measurement differs from count matrix: {name}")
    flags = {"zero_counts": totals == 0,
             "low_genes": detected < policy["min_genes_inclusive"],
             "high_genes": detected >= policy["max_genes_exclusive"],
             "high_mito": percentages >= policy["max_mito_pct_exclusive"]}
    for name, expected in flags.items():
        if cells[name].dtype != bool or not np.array_equal(cells[name].to_numpy(), expected):
            raise ValueError(f"Cell QC flag differs from recorded policy: {name}")
    passed = ~np.logical_or.reduce(list(flags.values()))
    reasons = [";".join(name for name in REASONS if flags[name][index]) for index in range(len(cells))]
    if (cells.qc_pass.dtype != bool or not np.array_equal(cells.qc_pass.to_numpy(), passed)
            or cells.exclusion_reasons.tolist() != reasons):
        raise ValueError("Cell QC eligibility or exclusion union differs")
    identity = {"release_id": metadata["release_id"], "dataset_id": manifest["dataset_id"],
                "dataset_version": manifest["dataset_version"],
                "policy_id": policy["policy_id"], "policy_version": policy["policy_version"]}
    for key in ("dataset_id", "dataset_version", "policy_id", "policy_version"):
        if not cells[key].eq(identity[key]).all():
            raise ValueError(f"Cell identity differs: {key}")
    for key in ("dataset_id", "dataset_version"):
        if not genes[key].eq(identity[key]).all():
            raise ValueError(f"Gene identity differs: {key}")
    expected_ids = (manifest["dataset_id"] + ":" + manifest["dataset_version"] + ":" + cells.barcode).tolist()
    require_equal(cells.cell_id.tolist(), expected_ids, "Stable cell identifiers")
    exclusions = {name: int(values.sum()) for name, values in flags.items()}
    metrics = {"cells_total": len(cells), "cells_pass": int(passed.sum()),
               "cells_fail": int((~passed).sum()), "features_total": len(genes),
               "total_counts": int(totals.sum()), "mitochondrial_features": int(mito.sum()),
               "qc_pass_fraction": float(passed.mean())}
    summary = {**{k: identity[k] for k in identity if k != "release_id"},
               **{k: metrics[k] for k in ("cells_total", "cells_pass", "cells_fail", "features_total", "total_counts")},
               **{f"cells_{k}": v for k, v in exclusions.items()}}
    require_equal(metadata["summary"], summary, "Release summary")
    metric_row = {**identity, **metrics, **{f"cells_{k}": v for k, v in exclusions.items()},
                  "mean_counts_per_cell": float(totals.mean()),
                  "mean_genes_per_cell": float(detected.mean()),
                  "mean_pct_counts_mito": float(np.nanmean(percentages)) if np.isfinite(percentages).any() else None}
    exclusion_rows = [{**identity, "exclusion_reason": name, "excluded_cells": count}
                      for name, count in exclusions.items()]
    cohort = cells.loc[passed].copy()
    cohort.insert(0, "release_id", metadata["release_id"])
    return metrics, exclusions, metric_row, exclusion_rows, cohort


def compare_aggregate(actual, expected, label):
    if set(actual) != set(expected):
        raise ValueError(f"{label} columns differ")
    for key, value in expected.items():
        candidate = actual[key]
        if isinstance(value, float):
            # Distributed SQL AVG and NumPy can round the same mean differently.
            # Keep the contract's pass fraction tolerance strictly absolute.
            relative_tolerance = 1e-12 if key.startswith("mean_") else 0
            if (isinstance(candidate, bool) or not isinstance(candidate, (int, float))
                    or not math.isfinite(candidate)
                    or not math.isclose(candidate, value, rel_tol=relative_tolerance, abs_tol=1e-12)):
                raise ValueError(f"{label} differs: {key} (actual={candidate!r}, expected={value!r})")
        elif type(value) is int:
            if type(candidate) is not int or candidate != value:
                raise ValueError(f"{label} differs: {key}")
        else:
            require_equal(candidate, value, f"{label}.{key}")


def evidence_contract(package, project, bucket, release_id, dbt_run_id, dataset):
    """Bind references to caller-selected destinations before making any network calls."""
    artifacts = {"release_manifest": f"processed/releases/{release_id}/release.json",
                 "dbt_audit": f"runs/cloud_dbt/{dbt_run_id}.json",
                 "dbt_manifest": f"runs/dbt/{dbt_run_id}/manifest.json",
                 "dbt_run_results": f"runs/dbt/{dbt_run_id}/run_results.json"}
    tables = {f"{project}.{dataset}.mart_dataset_qc", f"{project}.{dataset}.mart_qc_exclusions"}
    sources = {}
    for ref, source in package["evidence"].items():
        kind = source["kind"]
        if kind == "bigquery_table":
            if source["table_id"] not in tables:
                raise ValueError("Evidence table is outside the selected analytics marts")
        else:
            if kind in sources:
                raise ValueError(f"Ambiguous evidence kind: {kind}")
            if source["sha256"] == "0" * 64:
                raise ValueError("Placeholder evidence checksum is not valid")
            uri = source["uri"]
            if not uri.startswith(f"gs://{bucket}/"):
                raise ValueError("Evidence bucket differs from the selected bucket")
            if kind in artifacts and uri != f"gs://{bucket}/{artifacts[kind]}":
                raise ValueError(f"Evidence path differs: {kind}")
            sources[kind] = ref
    if not set(artifacts).issubset(sources):
        raise ValueError("Required release or dbt evidence is missing")
    sections = [(package["dataset"], {"release_manifest"}),
                (package["qc_policy"], {"release_manifest"}),
                (package["results"], {"release_manifest", "bigquery_table"}),
                (package["results"]["exclusions"], {"release_manifest", "bigquery_table"}),
                (package["dbt"], {"dbt_audit", "dbt_manifest", "dbt_run_results"})]
    for section, required in sections:
        refs = section["evidence_refs"]
        if any(ref not in package["evidence"] for ref in refs):
            raise ValueError("Unresolved evidence reference")
        if not required.issubset({package["evidence"][ref]["kind"] for ref in refs}):
            raise ValueError("Section evidence has the wrong source kinds")
    for section, name in ((package["results"], "mart_dataset_qc"),
                          (package["results"]["exclusions"], "mart_qc_exclusions")):
        if f"{project}.{dataset}.{name}" not in {
                package["evidence"][ref].get("table_id") for ref in section["evidence_refs"]}:
            raise ValueError("Result evidence refers to the wrong mart")
    return sources


def save_validation(root, audit, verified=None):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / audit["validation_id"]
    with tempfile.TemporaryDirectory(prefix=".validation-", dir=root) as temp:
        staging = Path(temp) / "result"
        staging.mkdir()
        if verified is not None:
            payload = (json.dumps(verified, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
            rendered = render_report(verified).encode()
            (staging / "knowledge.json").write_bytes(payload)
            (staging / "report.md").write_bytes(rendered)
            audit["outputs"] = {"knowledge.json": hashlib.sha256(payload).hexdigest(),
                                "report.md": hashlib.sha256(rendered).hexdigest()}
        (staging / "validation.json").write_text(json.dumps(audit, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        staging.rename(destination)
    return destination


def run_validate(package_path, project, bucket, release_id, dbt_run_id,
                 raw_dataset="pbmc_raw", analytics_base="pbmc_analytics", location="europe-west1",
                 output_dir="artifacts/knowledge_validation", storage_client=None, bigquery_client=None):
    audit = {"validation_id": uuid.uuid4().hex, "started_at": utc_now(), "status": "running",
             "project": project, "bucket": bucket, "release_id": release_id, "dbt_run_id": dbt_run_id,
             "raw_dataset": raw_dataset, "analytics_base": analytics_base, "location": location,
             "input_path": str(Path(package_path).resolve()),
             "numeric_tolerances": {"fraction_absolute": 1e-12, "mean_absolute": 1e-12,
                                    "mean_relative": 1e-12, "counts": "exact"},
             "checks_passed": [], "objects": {}, "tables": {},
             "validator_sha256": sha256_file(__file__), "schema_sha256": sha256_file(SCHEMA_PATH)}
    verified, failure = None, None
    try:
        validate_destination(bucket, project)
        for value in (raw_dataset, analytics_base):
            validate_identifier(value, "dataset")
        if not re.fullmatch(r"[a-f0-9]{64}", release_id) or not re.fullmatch(r"[a-f0-9]{32}", dbt_run_id):
            raise ValueError("Explicit full release ID and cloud dbt run ID are required")
        if not re.fullmatch(r"[A-Za-z0-9-]+", location):
            raise ValueError("Invalid location")
        payload = Path(package_path).read_bytes()
        audit["input_sha256"] = hashlib.sha256(payload).hexdigest()
        package = strict_json(payload)
        validate_structure(package)
        if package["package_status"] != "draft":
            raise ValueError("Validation requires a draft; a status label is not proof")
        require_equal(package["identity"]["release_id"], release_id, "Selected release ID")
        if package["limitations"] != LIMITATIONS:
            raise ValueError("Required project scientific limitations were changed")
        dataset = f"{analytics_base}_r_{release_id}"
        sources = evidence_contract(package, project, bucket, release_id, dbt_run_id, dataset)
        audit["checks_passed"].append("schema_identity_references_and_limitations")
        if storage_client is None:
            from google.cloud import storage
            storage_client = storage.Client(project=project)
        if bigquery_client is None:
            from google.cloud import bigquery
            bigquery_client = bigquery.Client(project=project, location=location)
        remote = storage_client.bucket(bucket)
        with tempfile.TemporaryDirectory(prefix="pbmc-validate-knowledge-") as temp:
            root = Path(temp)
            files = {}
            for ref, spec in package["evidence"].items():
                if spec["kind"] == "bigquery_table":
                    continue
                name = spec["uri"][len(f"gs://{bucket}/"):]
                blob = remote.get_blob(name, timeout=120)
                if blob is None:
                    raise ValueError(f"Evidence object is missing: {ref}")
                path = root / f"{ref}.json"
                generation = download_verified(blob, path, spec["sha256"])
                audit["objects"][name] = {"generation": generation, "sha256": spec["sha256"]}
                files[spec["kind"]] = path
            metadata = strict_json(files["release_manifest"].read_bytes())
            require_equal(set(metadata["outputs"]), set(OUTPUTS), "Release output names")
            for name, spec in metadata["outputs"].items():
                object_name = f"processed/releases/{release_id}/{name}"
                blob = remote.get_blob(object_name, timeout=120)
                if blob is None:
                    raise ValueError(f"Release output is missing: {name}")
                audit["objects"][object_name] = {"generation": int(blob.generation), "sha256": spec["sha256"]}
            download_release(remote, release_id, root / "processed")
            plan = prepare_load(root / "processed", release_id)
            directory = root / "processed" / "releases" / release_id
            require_equal(sha256_file(directory / "release.json"), package["evidence"][sources["release_manifest"]]["sha256"], "Restored manifest hash")
            config, policy = (metadata["identity"][key] for key in ("dataset_manifest", "qc_policy"))
            require_equal(package["identity"], {"release_id": release_id, "dataset_id": config["dataset_id"],
                                               "dataset_version": config["dataset_version"]}, "Package identity")
            expected_dataset = {key: config[key] for key in package["dataset"] if key not in
                                ("evidence_refs", "original_source_url", "source_sha256")}
            expected_dataset.update(original_source_url=config["original_source"], source_sha256=config["sha256"])
            require_equal({k: v for k, v in package["dataset"].items() if k != "evidence_refs"}, expected_dataset, "Dataset description")
            require_equal({k: v for k, v in package["qc_policy"].items() if k != "evidence_refs"},
                          {k: policy[k] for k in package["qc_policy"] if k != "evidence_refs"}, "Recorded QC policy")
            for kind, expected in (("dataset_config", config), ("qc_policy", policy)):
                if kind in files:
                    require_equal(yaml.safe_load(files[kind].read_text(encoding="utf-8")), expected, f"Optional {kind} evidence")
            audit["checks_passed"].append("artifact_checksums_release_identity_and_recorded_configuration")
            metrics, exclusions, metric_row, exclusion_rows, cohort = recompute_release(directory, metadata)
            compare_aggregate(package["results"]["metrics"], metrics, "Package metrics")
            require_equal(package["results"]["exclusions"]["counts"], exclusions, "Package exclusions")
            audit["checks_passed"].append("independent_matrix_metrics_policy_flags_and_exclusion_union")
            target = root / "dbt"
            target.mkdir()
            for kind, name in (("dbt_manifest", "manifest.json"), ("dbt_run_results", "run_results.json")):
                (target / name).write_bytes(files[kind].read_bytes())
            summary = summarize_results(target, dbt_run_id, release_id, dataset)
            dbt_manifest = strict_json(files["dbt_manifest"].read_bytes())
            for node in dbt_manifest["nodes"].values():
                if node["resource_type"] == "model":
                    require_equal(node.get("database"), project, "dbt model project")
            dbt_audit = strict_json(files["dbt_audit"].read_bytes())
            for key, value in {"status": "success", "run_id": dbt_run_id, "project": project,
                               "bucket": bucket, "release_id": release_id, "raw_dataset": raw_dataset,
                               "location": location}.items():
                require_equal(dbt_audit.get(key), value, f"dbt audit {key}")
            release_source = package["evidence"][sources["release_manifest"]]
            require_equal(dbt_audit["input_manifest"], {k: release_source[k] for k in ("uri", "sha256")}, "dbt source manifest")
            require_equal(dbt_audit["result"], {**summary,
                          "audit_uri": f"gs://{bucket}/runs/cloud_dbt/{dbt_run_id}.json",
                          "artifacts_uri": f"gs://{bucket}/runs/dbt/{dbt_run_id}/",
                          "cloud_execution": dbt_audit.get("cloud_execution")}, "dbt result summary")
            require_equal({k: v for k, v in package["dbt"].items() if k != "evidence_refs"},
                          {"invocation_id": summary["dbt_invocation_id"], "status": "success",
                           "models_succeeded": summary["models"], "tests_passed": summary["tests"]}, "Package dbt summary")
            audit["checks_passed"].append("successful_matching_dbt_invocation_and_complete_results")
            for ds in (raw_dataset, dataset):
                if bigquery_client.get_dataset(f"{project}.{ds}").location.lower() != location.lower():
                    raise ValueError("Warehouse dataset location differs")

            def fingerprint(table):
                if table.table_type != "TABLE" or not table.etag:
                    raise ValueError("Validation requires materialized tables with version metadata")
                return {"etag": table.etag, "modified": str(table.modified), "rows": table.num_rows}

            def read_table(table_id, maximum):
                table = bigquery_client.get_table(table_id)
                audit["tables"][table_id] = fingerprint(table)
                if table.num_rows > maximum:
                    raise ValueError(f"Unexpected table cardinality: {table_id}")
                rows = [dict(row.items()) for row in bigquery_client.list_rows(table_id, max_results=maximum + 1)]
                if len(rows) != table.num_rows:
                    raise ValueError("Table row count changed during read")
                return table, rows

            for name, spec in plan["tables"].items():
                table_id = f"{project}.{raw_dataset}.{spec['table_name']}"
                table, rows = read_table(table_id, spec["rows"])
                require_equal(table.description, table_description(plan, name), "Raw table description")
                require_equal([(f.name, f.field_type, f.mode) for f in table.schema],
                              [(f["name"], f["type"], "NULLABLE") for f in spec["schema"]], "Raw table schema")
                compare_rows(name, pd.DataFrame(rows), pd.read_parquet(spec["file"]))
            _, rows = read_table(f"{project}.{dataset}.mart_dataset_qc", 1)
            if len(rows) != 1:
                raise ValueError("Expected exactly one QC aggregate row")
            compare_aggregate(rows[0], metric_row, "QC mart")
            _, rows = read_table(f"{project}.{dataset}.mart_qc_exclusions", 4)
            if len(rows) != 4 or len({row.get("exclusion_reason") for row in rows}) != 4:
                raise ValueError("Exclusion categories are missing or duplicated")
            for actual, expected in zip(sorted(rows, key=lambda x: x["exclusion_reason"]),
                                        sorted(exclusion_rows, key=lambda x: x["exclusion_reason"])):
                compare_aggregate(actual, expected, "Exclusion mart")
            table, rows = read_table(f"{project}.{dataset}.mart_cohort_cells", len(cohort))
            require_equal(set(field.name for field in table.schema), set(cohort.columns), "Cohort columns")
            actual = pd.DataFrame(rows) if rows else pd.DataFrame(columns=cohort.columns)
            compare_rows("cells", actual, cohort)
            audit["checks_passed"].append("complete_raw_tables_recomputed_marts_and_exact_cohort_membership")
            for name, recorded in audit["objects"].items():
                blob = remote.get_blob(name, timeout=120)
                if blob is None or int(blob.generation) != recorded["generation"]:
                    raise ValueError("GCS evidence changed during validation")
            for name, recorded in audit["tables"].items():
                require_equal(fingerprint(bigquery_client.get_table(name)), recorded, "Table version at end of validation")
            audit["checks_passed"].append("object_generations_and_table_versions_stable_during_validation")
            verified = deepcopy(package)
            verified["package_status"] = "verified"
            verified["limitations"] = list(LIMITATIONS[1:])
            validate_structure(verified)
            audit.update(status="verified", metrics=metrics, exclusions=exclusions,
                         cohort_rows=len(cohort), dbt_invocation_id=summary["dbt_invocation_id"])
    except Exception as error:
        failure = error
        verified = None
        audit.update(status="failed", error=f"{type(error).__name__}: {error}")
    audit["finished_at"] = utc_now()
    destination = save_validation(output_dir, audit, verified)
    if failure is not None:
        raise ValueError(f"Validation failed: {failure}; report: {destination / 'validation.json'}") from failure
    return {"status": "verified", "validation_id": audit["validation_id"],
            "package_path": str(destination / "knowledge.json"),
            "report_path": str(destination / "report.md"), "validation_path": str(destination / "validation.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("package-path", "project", "bucket", "release-id", "dbt-run-id"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--raw-dataset", default="pbmc_raw")
    parser.add_argument("--analytics-base", default="pbmc_analytics")
    parser.add_argument("--location", default="europe-west1")
    parser.add_argument("--output-dir", default="artifacts/knowledge_validation")
    try:
        print(json.dumps(run_validate(**vars(parser.parse_args())), indent=2))
    except Exception as error:
        parser.exit(1, f"Knowledge validation failed: {error}\n")


if __name__ == "__main__":
    main()
