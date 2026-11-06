"""Generate a local draft knowledge package from an explicit cloud release and dbt run."""

import argparse
import json
import math
import re
import tempfile
from pathlib import Path

from jsonschema import Draft202012Validator

from .cloud_release import download_release, download_verified
from .ingestion import sha256_file
from .load_warehouse import prepare_load, validate_identifier
from .publish_gcs import validate_destination
from .run_cloud_dbt import summarize_results


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas/pbmc-knowledge-v1.schema.json"
METRICS = ("cells_total", "cells_pass", "cells_fail", "features_total", "total_counts",
           "mitochondrial_features", "qc_pass_fraction")
REASONS = ("zero_counts", "low_genes", "high_genes", "high_mito")
LIMITATIONS = [
    "Draft package: independent source-aware validation in Step 5.3 is still required.",
    "The dataset contains one healthy donor and does not support disease comparisons.",
    "This pipeline does not infer cell types.",
    "QC exclusion reasons can overlap; their counts must not be assumed to sum to cells_fail.",
]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_structure(package):
    # Reject NaN/Infinity, which Python JSON otherwise accepts as extensions.
    json.dumps(package, allow_nan=False)
    Draft202012Validator(read_json(SCHEMA_PATH)).validate(package)


def assemble_package(metadata, metric_rows, exclusion_rows, dbt_summary, evidence):
    """Map checked sources into the contract; do not promote a draft to verified."""
    release_id = metadata["release_id"]
    manifest, policy = (metadata["identity"][name] for name in ("dataset_manifest", "qc_policy"))
    identity = {"release_id": release_id, "dataset_id": manifest["dataset_id"],
                "dataset_version": manifest["dataset_version"], "policy_id": policy["policy_id"],
                "policy_version": policy["policy_version"]}
    if len(metric_rows) != 1:
        raise ValueError("Expected exactly one dataset QC row")
    if len(exclusion_rows) != len(REASONS):
        raise ValueError("Expected exactly four exclusion rows")
    for row in [*metric_rows, *exclusion_rows]:
        if any(row.get(key) != value for key, value in identity.items()):
            raise ValueError("Analytics row belongs to a different release, dataset or policy")
    exclusions = {}
    for row in exclusion_rows:
        reason = row["exclusion_reason"]
        if reason not in REASONS or reason in exclusions:
            raise ValueError("Unexpected or duplicate exclusion reason")
        exclusions[reason] = row["excluded_cells"]
    metrics = {name: metric_rows[0][name] for name in METRICS}
    for name in ("cells_total", "cells_pass", "cells_fail", "features_total", "total_counts"):
        if metrics[name] != metadata["summary"][name]:
            raise ValueError(f"Analytics and release disagree: {name}")
    for reason in REASONS:
        if exclusions[reason] != metadata["summary"][f"cells_{reason}"]:
            raise ValueError(f"Analytics and release disagree: {reason}")
    if (metrics["cells_total"] != metrics["cells_pass"] + metrics["cells_fail"]
            or metrics["cells_total"] <= 0
            or not math.isclose(metrics["qc_pass_fraction"],
                                metrics["cells_pass"] / metrics["cells_total"],
                                rel_tol=0, abs_tol=1e-12)):
        raise ValueError("QC totals or passing fraction do not reconcile")
    if manifest["donor_count"] != 1:
        raise ValueError("This first-release knowledge contract supports one donor")
    package = {
        "format_name": "pbmc-project-knowledge", "schema_version": "1.0.0",
        "package_status": "draft",
        "identity": {key: identity[key] for key in ("dataset_id", "dataset_version", "release_id")},
        "dataset": {
            **{name: manifest[name] for name in ("organism", "tissue", "donor_count",
                                                "reference_genome", "source_url", "license", "attribution")},
            "original_source_url": manifest["original_source"], "source_sha256": manifest["sha256"],
            "evidence_refs": ["release"],
        },
        "qc_policy": {
            **{name: policy[name] for name in ("policy_id", "policy_version", "min_genes_inclusive",
                                             "max_genes_exclusive", "max_mito_pct_exclusive",
                                             "mitochondrial_prefix")},
            "evidence_refs": ["release"],
        },
        "results": {"metrics": metrics, "evidence_refs": ["dataset_qc", "release"],
                    "exclusions": {"counts": {reason: exclusions[reason] for reason in REASONS},
                                   "reasons_may_overlap": True,
                                   "evidence_refs": ["qc_exclusions", "release"]}},
        "dbt": {"invocation_id": dbt_summary["dbt_invocation_id"], "status": "success",
                "models_succeeded": dbt_summary["models"], "tests_passed": dbt_summary["tests"],
                "evidence_refs": ["dbt_audit", "dbt_manifest", "dbt_results"]},
        "evidence": evidence, "limitations": list(LIMITATIONS),
    }
    validate_structure(package)
    sections = [package["dataset"], package["qc_policy"], package["results"],
                package["results"]["exclusions"], package["dbt"]]
    if any(ref not in evidence for section in sections for ref in section["evidence_refs"]):
        raise ValueError("An evidence reference does not resolve")
    return package


def render_report(package):
    """Render facts exclusively from the package, without a second data source or LLM."""
    validate_structure(package)
    # Match on-disk sorted JSON even if the caller supplied another mapping order.
    package = json.loads(json.dumps(package, sort_keys=True, allow_nan=False))

    def text(value):
        return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("`", "\\`").replace("\n", " ").replace("\r", " ")

    def fields(title, values, evidence_refs=None):
        rows = [f"## {title}", "", "| Field | Value |", "| --- | --- |"]
        rows.extend(f"| {text(key)} | {text(value)} |" for key, value in values.items())
        rows.append("")
        if evidence_refs:
            rows.extend(["Evidence: " + ", ".join(f"[{ref}](#evidence)" for ref in evidence_refs), ""])
        return rows

    # Version-1 reports are checksum-bound publication artifacts. Keep their
    # historical wording byte-compatible; current app branding is independent.
    lines = ["# PBMC knowledge report", "", f"Package status: **{package['package_status']}**.",
             "This project-defined package is not an external OKF standard.", ""]
    lines += fields("Identity", {"format_name": package["format_name"],
                                  "schema_version": package["schema_version"], **package["identity"]})
    for key, title in (("dataset", "Dataset"), ("qc_policy", "QC policy"), ("dbt", "dbt build")):
        section = package[key]
        lines += fields(title, {k: v for k, v in section.items() if k != "evidence_refs"}, section["evidence_refs"])
    lines += fields("QC results", package["results"]["metrics"], package["results"]["evidence_refs"])
    exclusions = package["results"]["exclusions"]
    lines += fields("QC exclusions", {**exclusions["counts"],
                                      "reasons_may_overlap": exclusions["reasons_may_overlap"]},
                    exclusions["evidence_refs"])
    lines += ["## Limitations", "", *[f"- {text(item)}" for item in package["limitations"]], "", "## Evidence", ""]
    for ref, source in package["evidence"].items():
        lines += [f"### {ref}", ""]
        lines += [f"- {text(key)}: {text(value)}" for key, value in source.items()]
        lines += [""]
    return "\n".join(lines)


def write_package(package, output_dir):
    """Publish the two local files as one directory; refuse conflicting prior output."""
    validate_structure(package)
    parent = Path(output_dir).resolve() / package["identity"]["release_id"]
    destination = parent / package["dbt"]["invocation_id"]
    # Invocation IDs become path components, so reject arbitrary paths.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", package["dbt"]["invocation_id"]):
        raise ValueError("Unsafe dbt invocation ID")
    payloads = {"knowledge.json": json.dumps(package, indent=2, sort_keys=True, allow_nan=False) + "\n",
                "report.md": render_report(package)}
    if destination.exists():
        if any(not (destination / name).is_file() or
               (destination / name).read_bytes() != payload.encode("utf-8")
               for name, payload in payloads.items()):
            raise FileExistsError(f"Knowledge output differs; refusing overwrite: {destination}")
        return destination
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".knowledge-", dir=parent) as temp:
        staging = Path(temp) / "package"
        staging.mkdir()
        for name, payload in payloads.items():
            (staging / name).write_bytes(payload.encode("utf-8"))
        staging.rename(destination)
    return destination


def run_generate(project, bucket, release_id, dbt_run_id, location="europe-west1",
                 analytics_base="pbmc_analytics", output_dir="artifacts/knowledge",
                 storage_client=None, bigquery_client=None):
    """Read cloud inputs and write locally; this function makes no cloud mutations."""
    validate_destination(bucket, project)
    validate_identifier(analytics_base, "dataset")
    if not re.fullmatch(r"[a-f0-9]{64}", release_id):
        raise ValueError("Expected an explicit full release SHA-256")
    if not re.fullmatch(r"[a-f0-9]{32}", dbt_run_id):
        raise ValueError("Expected an explicit cloud dbt run ID (32 lowercase hex characters)")
    if not re.fullmatch(r"[a-zA-Z0-9-]+", location):
        raise ValueError("Invalid location")
    if storage_client is None:
        from google.cloud import storage
        storage_client = storage.Client(project=project)
    if bigquery_client is None:
        from google.cloud import bigquery
        bigquery_client = bigquery.Client(project=project, location=location)
    remote = storage_client.bucket(bucket)
    dataset = f"{analytics_base}_r_{release_id}"
    with tempfile.TemporaryDirectory(prefix="pbmc-knowledge-") as temp:
        root = Path(temp)
        evidence = {}

        def artifact(ref, kind, object_name):
            blob = remote.get_blob(object_name, timeout=120)
            if blob is None:
                raise ValueError(f"Required evidence is missing: {object_name}")
            path = root / f"{ref}.json"
            download_verified(blob, path)
            evidence[ref] = {"kind": kind, "uri": f"gs://{bucket}/{object_name}",
                             "sha256": sha256_file(path)}
            return path

        audit = read_json(artifact("dbt_audit", "dbt_audit", f"runs/cloud_dbt/{dbt_run_id}.json"))
        if (audit.get("status") != "success" or audit.get("run_id") != dbt_run_id
                or audit.get("project") != project or audit.get("bucket") != bucket
                or audit.get("release_id") != release_id or audit.get("location", "").lower() != location.lower()):
            raise ValueError("Cloud dbt audit is unsuccessful or belongs to different inputs")
        download_release(remote, release_id, root / "processed")
        prepare_load(root / "processed", release_id)
        marker = root / "processed" / "releases" / release_id / "release.json"
        evidence["release"] = {"kind": "release_manifest",
                               "uri": f"gs://{bucket}/processed/releases/{release_id}/release.json",
                               "sha256": sha256_file(marker)}
        if audit.get("input_manifest") != {k: evidence["release"][k] for k in ("uri", "sha256")}:
            raise ValueError("dbt audit refers to a different input manifest")
        target = root / "dbt"
        target.mkdir()
        for ref, name, kind in (("dbt_manifest", "manifest.json", "dbt_manifest"),
                                ("dbt_results", "run_results.json", "dbt_run_results")):
            path = artifact(ref, kind, f"runs/dbt/{dbt_run_id}/{name}")
            (target / name).write_bytes(path.read_bytes())
        summary = summarize_results(target, dbt_run_id, release_id, dataset)
        expected_result = {**summary, "audit_uri": evidence["dbt_audit"]["uri"],
                           "artifacts_uri": f"gs://{bucket}/runs/dbt/{dbt_run_id}/",
                           "cloud_execution": audit.get("cloud_execution")}
        if audit.get("result") != expected_result:
            raise ValueError("dbt audit result differs from downloaded artifacts")
        if bigquery_client.get_dataset(f"{project}.{dataset}").location.lower() != location.lower():
            raise ValueError("Analytics dataset is in a different location")
        rows = {}
        for ref, name, limit in (("dataset_qc", "mart_dataset_qc", 2),
                                 ("qc_exclusions", "mart_qc_exclusions", 5)):
            table = f"{project}.{dataset}.{name}"
            evidence[ref] = {"kind": "bigquery_table", "table_id": table}
            rows[ref] = [dict(row.items()) for row in bigquery_client.list_rows(table, max_results=limit)]
        package = assemble_package(read_json(marker), rows["dataset_qc"], rows["qc_exclusions"], summary, evidence)
        destination = write_package(package, output_dir)
    return {"status": "draft", "release_id": release_id, "dbt_run_id": dbt_run_id,
            "package_path": str(destination / "knowledge.json"), "report_path": str(destination / "report.md")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("project", "bucket", "release-id", "dbt-run-id"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--location", default="europe-west1")
    parser.add_argument("--analytics-base", default="pbmc_analytics")
    parser.add_argument("--output-dir", default="artifacts/knowledge")
    try:
        print(json.dumps(run_generate(**vars(parser.parse_args())), indent=2))
    except Exception as error:
        parser.exit(1, f"Knowledge generation failed: {error}\n")


if __name__ == "__main__":
    main()
