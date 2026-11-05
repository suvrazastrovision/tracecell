"""Publish a trusted verified knowledge bundle to GCS with a create-only commit marker."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import uuid

from . import validate_knowledge as validator
from .generate_knowledge import LIMITATIONS, SCHEMA_PATH, render_report, validate_structure
from .ingestion import canonical_json, sha256_file
from .load_warehouse import validate_identifier
from .publish_gcs import publish_object, validate_destination
from .run_qc import atomic_json


BUNDLE_FILES = ("knowledge.json", "report.md", "validation.json")
REQUIRED_CHECKS = [
    "schema_identity_references_and_limitations",
    "artifact_checksums_release_identity_and_recorded_configuration",
    "independent_matrix_metrics_policy_flags_and_exclusion_union",
    "successful_matching_dbt_invocation_and_complete_results",
    "complete_raw_tables_recomputed_marts_and_exact_cohort_membership",
    "object_generations_and_table_versions_stable_during_validation",
]


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def prepare_bundle(validation_dir, project, bucket, release_id, snapshot_dir):
    """Validate trusted local validator output and capture the exact bytes to publish."""
    validate_destination(bucket, project)
    if not re.fullmatch(r"[a-f0-9]{64}", release_id):
        raise ValueError("Expected an explicit full release SHA-256")
    directory = Path(validation_dir)
    payloads = {name: (directory / name).read_bytes() for name in BUNDLE_FILES}
    package = validator.strict_json(payloads["knowledge.json"])
    audit = validator.strict_json(payloads["validation.json"])
    validate_structure(package)
    if package["package_status"] != "verified" or audit.get("status") != "verified":
        raise ValueError("Only successful verified bundles can be published")
    for name, expected in (("project", project), ("bucket", bucket), ("release_id", release_id)):
        validator.require_equal(audit.get(name), expected, f"Validation destination {name}")
    validator.require_equal(package["identity"]["release_id"], release_id, "Package release")
    validation_id, dbt_run_id = audit.get("validation_id"), audit.get("dbt_run_id")
    for value in (validation_id, dbt_run_id):
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
            raise ValueError("Validation and dbt run IDs must be 32 lowercase hex characters")
    validator.require_equal(audit.get("validator_sha256"), sha256_file(validator.__file__), "Validator code hash; revalidate after code changes")
    validator.require_equal(audit.get("schema_sha256"), sha256_file(SCHEMA_PATH), "Schema hash; revalidate after schema changes")
    validator.require_equal(audit.get("checks_passed"), REQUIRED_CHECKS, "Completed validation checks")
    validator.require_equal(audit.get("outputs"), {name: digest(payloads[name]) for name in ("knowledge.json", "report.md")}, "Validated output hashes")
    if payloads["report.md"] != render_report(package).encode("utf-8"):
        raise ValueError("Readable report does not reproduce from the verified package")
    validator.require_equal(package["limitations"], LIMITATIONS[1:], "Scientific limitations")
    validator.require_equal(audit.get("metrics"), package["results"]["metrics"], "Validated metrics")
    validator.require_equal(audit.get("exclusions"), package["results"]["exclusions"]["counts"], "Validated exclusions")
    validator.require_equal(audit.get("cohort_rows"), package["results"]["metrics"]["cells_pass"], "Validated cohort count")
    validator.require_equal(audit.get("dbt_invocation_id"), package["dbt"]["invocation_id"], "Validated dbt invocation")
    analytics_base = validate_identifier(audit["analytics_base"], "dataset")
    dataset = f"{analytics_base}_r_{release_id}"
    validator.evidence_contract(package, project, bucket, release_id, dbt_run_id, dataset)
    # Bind every artifact cited by the verified package to the generations checked
    # by the validator, rather than trusting a manually changed status label.
    for source in package["evidence"].values():
        if source["kind"] == "bigquery_table":
            if source["table_id"] not in audit.get("tables", {}):
                raise ValueError("Evidence table was not recorded by validation")
            continue
        name = source["uri"][len(f"gs://{bucket}/"):]
        checked = audit.get("objects", {}).get(name, {})
        validator.require_equal(checked.get("sha256"), source["sha256"], "Validated evidence checksum")
        if type(checked.get("generation")) is not int or checked["generation"] <= 0:
            raise ValueError("Validated evidence generation is missing")
    identity = {
        "format_name": package["format_name"], "schema_version": package["schema_version"],
        "dataset_id": package["identity"]["dataset_id"],
        "dataset_version": package["identity"]["dataset_version"], "release_id": release_id,
        "validation_id": validation_id, "dbt_run_id": dbt_run_id,
        "dbt_invocation_id": package["dbt"]["invocation_id"],
        "files": {name: {"sha256": digest(payload), "bytes": len(payload)} for name, payload in payloads.items()},
    }
    package_id = digest(canonical_json(identity).encode("utf-8"))
    prefix = f"knowledge/releases/{release_id}/{package_id}"
    snapshot = Path(snapshot_dir)
    snapshot.mkdir(parents=True, exist_ok=True)
    objects = {}
    for name, payload in payloads.items():
        path = snapshot / name
        path.write_bytes(payload)
        objects[name] = {"file": str(path), "object_name": f"{prefix}/{name}",
                         **identity["files"][name]}
    return {"project": project, "bucket": bucket, "release_id": release_id,
            "package_id": package_id, "identity": identity, "prefix": prefix,
            "publication_uri": f"gs://{bucket}/{prefix}/publication.json", "objects": objects}


def run_publish_knowledge(validation_dir, project, bucket, release_id,
                          artifact_dir="artifacts", dry_run=False, client=None):
    audit_id = uuid.uuid4().hex
    runs = Path(artifact_dir) / "knowledge_publication_runs"
    runs.mkdir(parents=True, exist_ok=True)
    audit_path = runs / f"{audit_id}.json"
    audit_object = f"runs/knowledge_publication/{audit_id}.json"
    audit = {"run_id": audit_id, "started_at": validator.utc_now(), "status": "running",
             "project": project, "bucket": bucket, "release_id": release_id,
             "publisher_sha256": sha256_file(__file__)}
    remote, result, failure, audit_receipt = None, None, None, None
    try:
        with tempfile.TemporaryDirectory(prefix="pbmc-publish-knowledge-") as temp:
            plan = prepare_bundle(validation_dir, project, bucket, release_id, Path(temp) / "bundle")
            audit.update(package_id=plan["package_id"], validation_id=plan["identity"]["validation_id"])
            if dry_run:
                result = {key: plan[key] for key in ("project", "bucket", "release_id", "package_id", "identity", "publication_uri")}
                result["status"] = "dry_run"
                audit.update(status="dry_run", result=result)
            else:
                if client is None:
                    from google.cloud import storage
                    client = storage.Client(project=project)
                remote = client.bucket(bucket)
                objects = {}
                for name in BUNDLE_FILES:
                    spec = plan["objects"][name]
                    uploaded = publish_object(remote, spec)
                    objects[name] = {"uri": f"gs://{bucket}/{spec['object_name']}",
                                     "generation": uploaded["generation"], "sha256": uploaded["sha256"],
                                     "bytes": spec["bytes"]}
                    audit.setdefault("objects", {})[name] = uploaded
                marker = {"publication_schema_version": 1, "status": "published",
                          "package_id": plan["package_id"], "identity": plan["identity"], "objects": objects}
                path = Path(temp) / "publication.json"
                path.write_text(json.dumps(marker, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
                committed = publish_object(remote, {"file": str(path),
                    "object_name": f"{plan['prefix']}/publication.json", "sha256": sha256_file(path)})
                audit["objects"]["publication.json"] = committed
                result = {"status": "published", "release_id": release_id, "package_id": plan["package_id"],
                          "publication_uri": plan["publication_uri"], "publication": committed,
                          "reused": all(item["reused"] for item in audit["objects"].values()),
                          "objects": objects, "audit_uri": f"gs://{bucket}/{audit_object}"}
                audit.update(status="success", result=result)
    except Exception as error:
        failure = error
        audit.update(status="failed", error=f"{type(error).__name__}: {error}")
    audit["finished_at"] = validator.utc_now()
    atomic_json(audit_path, audit)
    print(canonical_json({"event": "knowledge_publication_run", **audit}), flush=True)
    if remote is not None:
        try:
            audit_receipt = publish_object(remote, {"file": str(audit_path), "object_name": audit_object,
                                                   "sha256": sha256_file(audit_path)})
        except Exception as error:
            audit.update(status="failed", audit_upload_error=f"{type(error).__name__}: {error}")
            atomic_json(audit_path, audit)
            print(canonical_json({"event": "knowledge_publication_audit_upload_failed", **audit}), file=sys.stderr, flush=True)
            if failure is None:
                failure = RuntimeError("Bundle committed, but publication audit upload failed")
    if failure is not None:
        raise RuntimeError(f"Knowledge publication failed: {failure}; local audit: {audit_path}") from failure
    return {**result, "local_audit_path": str(audit_path.resolve()), "audit_object": audit_receipt}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("validation-dir", "project", "bucket", "release-id"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--artifact-dir", default="artifacts")
    parser.add_argument("--dry-run", action="store_true", help="Check the local bundle and show its address without cloud calls")
    try:
        print(json.dumps(run_publish_knowledge(**vars(parser.parse_args())), indent=2))
    except Exception as error:
        parser.exit(1, f"Knowledge publication failed: {error}\n")


if __name__ == "__main__":
    main()
