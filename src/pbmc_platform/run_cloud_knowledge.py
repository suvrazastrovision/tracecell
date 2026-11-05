"""Generate, independently validate and publish knowledge from one execution-bound dbt run."""

import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from .generate_knowledge import run_generate
from .ingestion import canonical_json, sha256_file
from .knowledge_handoff import execution_name, publish_receipt, read_json_object, validate_dbt_receipt
from .load_warehouse import validate_identifier
from .publish_gcs import publish_object, validate_destination
from .publish_knowledge import BUNDLE_FILES, prepare_bundle, run_publish_knowledge
from .run_qc import write_json
from .validate_knowledge import run_validate, utc_now


def restore_completed(remote, root, receipt, project, bucket, release_id, execution, dbt_execution, run_id, handoff_generation):
    expected = {"schema_version": 1, "status": "success", "cloud_execution": execution,
                "project": project, "bucket": bucket, "release_id": release_id,
                "dbt_execution": dbt_execution, "dbt_run_id": run_id,
                "dbt_handoff_generation": int(handoff_generation)}
    if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in expected.items()):
        raise ValueError("Knowledge completion receipt belongs to different inputs")
    result = receipt["result"]
    package_id = result["package_id"]
    if not re.fullmatch(r"[a-f0-9]{64}", package_id):
        raise ValueError("Invalid completed knowledge package ID")
    prefix = f"knowledge/releases/{release_id}/{package_id}"
    if result["publication_uri"] != f"gs://{bucket}/{prefix}/publication.json":
        raise ValueError("Knowledge completion points outside selected release")
    marker, _ = read_json_object(remote, f"{prefix}/publication.json", root / "completed-publication.json",
                                 result["publication"]["generation"], result["publication"]["sha256"])
    if (marker.get("publication_schema_version") != 1 or marker.get("status") != "published"
            or marker.get("package_id") != package_id or result.get("status") != "published"
            or marker.get("identity", {}).get("dbt_run_id") != run_id
            or set(marker.get("objects", {})) != set(BUNDLE_FILES)):
        raise ValueError("Completed knowledge marker is invalid")
    bundle = root / "completed-bundle"
    bundle.mkdir()
    from .cloud_release import download_verified
    for name in BUNDLE_FILES:
        spec = marker["objects"][name]
        if spec["uri"] != f"gs://{bucket}/{prefix}/{name}":
            raise ValueError("Completed knowledge file points outside the package")
        blob = remote.get_blob(f"{prefix}/{name}", timeout=120)
        if blob is None or int(blob.generation) != spec["generation"]:
            raise ValueError("Completed knowledge object generation changed")
        download_verified(blob, bundle / name, spec["sha256"])
    plan = prepare_bundle(bundle, project, bucket, release_id, root / "completed-snapshot")
    if plan["package_id"] != package_id or plan["identity"] != marker["identity"]:
        raise ValueError("Completed knowledge identity differs")
    return result


def run_cloud_knowledge(project, bucket, release_id, dbt_run_id, dbt_execution, dbt_handoff_generation,
                         raw_dataset="pbmc_raw", analytics_base="pbmc_analytics", location="europe-west1",
                         work_dir=None, storage_client=None, bigquery_client=None):
    if work_dir is None:
        with tempfile.TemporaryDirectory(prefix="pbmc-cloud-knowledge-") as temp:
            return run_cloud_knowledge(project, bucket, release_id, dbt_run_id, dbt_execution, dbt_handoff_generation,
                                       raw_dataset, analytics_base, location, temp, storage_client, bigquery_client)
    root = Path(work_dir).resolve() / uuid.uuid4().hex
    root.mkdir(parents=True)
    audit = {"run_id": root.name, "status": "running", "started_at": utc_now(), "project": project,
             "bucket": bucket, "release_id": release_id, "dbt_run_id": dbt_run_id,
             "dbt_execution": dbt_execution, "dbt_handoff_generation": str(dbt_handoff_generation),
             "raw_dataset": raw_dataset, "analytics_base": analytics_base, "location": location,
             "cloud_execution": os.environ.get("CLOUD_RUN_EXECUTION")}
    remote, receipt = None, None
    try:
        validate_destination(bucket, project)
        execution = execution_name(audit["cloud_execution"])
        execution_name(dbt_execution)
        for value in (raw_dataset, analytics_base):
            validate_identifier(value, "dataset")
        if not re.fullmatch(r"[a-f0-9]{64}", release_id) or not re.fullmatch(r"[a-f0-9]{32}", dbt_run_id):
            raise ValueError("Explicit release and dbt run IDs required")
        if int(dbt_handoff_generation) <= 0:
            raise ValueError("Positive dbt receipt generation required")
        if storage_client is None:
            from google.cloud import storage
            storage_client = storage.Client(project=project)
        remote = storage_client.bucket(bucket)
        dbt_receipt, _ = read_json_object(remote, f"runs/handoffs/dbt/{dbt_execution}.json", root / "dbt-receipt.json",
                                          dbt_handoff_generation)
        result = validate_dbt_receipt(remote, root, dbt_receipt, project, bucket, release_id, dbt_execution,
                                      raw_dataset, f"{analytics_base}_r_{release_id}", location)
        if result["run_id"] != dbt_run_id:
            raise ValueError("Selected dbt run differs from execution receipt")
        existing_name = f"runs/handoffs/knowledge/{execution}.json"
        if remote.get_blob(existing_name, timeout=120) is not None:
            receipt, _ = read_json_object(remote, existing_name, root / "existing-completion.json")
            published = restore_completed(remote, root, receipt, project, bucket, release_id, execution,
                                           dbt_execution, dbt_run_id, dbt_handoff_generation)
            audit["reused"] = True
        else:
            generated = run_generate(project, bucket, release_id, dbt_run_id, location, analytics_base,
                                      root / "draft", storage_client, bigquery_client)
            validated = run_validate(generated["package_path"], project, bucket, release_id, dbt_run_id,
                                      raw_dataset, analytics_base, location, root / "validation", storage_client, bigquery_client)
            published = run_publish_knowledge(Path(validated["package_path"]).parent, project, bucket, release_id,
                                               artifact_dir=root / "artifacts", client=storage_client)
            receipt = {"schema_version": 1, "status": "success", "cloud_execution": execution,
                       "project": project, "bucket": bucket, "release_id": release_id,
                       "dbt_execution": dbt_execution, "dbt_run_id": dbt_run_id,
                       "dbt_handoff_generation": int(dbt_handoff_generation), "result": published}
            audit["reused"] = False
        audit.update(status="success", result=published)
        return published
    except Exception as error:
        audit.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        audit["finished_at"] = utc_now()
        path = root / "cloud-audit.json"
        write_json(path, audit)
        print(canonical_json({"event": "cloud_knowledge_run", **audit}), flush=True)
        failures = []
        if remote is not None:
            records = [(p, f"runs/knowledge_validation/{root.name}/{p.parent.name}.json")
                       for p in (root / "validation").glob("*/validation.json")]
            records += [(p, f"runs/knowledge_generation/{root.name}/{p.name}")
                        for p in (root / "draft").glob("*/*/*") if p.is_file()]
            records.append((path, f"runs/cloud_knowledge/{root.name}.json"))
            for local, name in records:
                try:
                    publish_object(remote, {"file": str(local), "object_name": name, "sha256": sha256_file(local)})
                except Exception as error:
                    failures.append(str(error))
            if not failures and audit["status"] == "success":
                try:
                    publish_receipt(remote, root, "knowledge", audit["cloud_execution"], receipt)
                except Exception as error:
                    failures.append(str(error))
        if failures:
            print(canonical_json({"event": "cloud_knowledge_artifact_upload_failed", "run_id": root.name,
                                   "errors": failures}), flush=True)
            if audit["status"] == "success":
                raise RuntimeError("Knowledge processing completed but durable artifacts/handoff failed: " + "; ".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("project", "bucket", "release-id", "dbt-run-id", "dbt-execution", "dbt-handoff-generation"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--raw-dataset", default="pbmc_raw")
    parser.add_argument("--analytics-base", default="pbmc_analytics")
    parser.add_argument("--location", default="europe-west1")
    try:
        print(json.dumps(run_cloud_knowledge(**vars(parser.parse_args())), indent=2))
    except Exception as error:
        parser.exit(1, f"Cloud knowledge failed: {error}\n")


if __name__ == "__main__":
    main()
