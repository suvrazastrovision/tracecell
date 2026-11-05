"""Generation-pinned execution receipts for the dbt-to-knowledge boundary."""

import json
from pathlib import Path
import re

from .cloud_release import download_verified
from .ingestion import sha256_file
from .run_cloud_dbt import summarize_results
from .run_qc import write_json
from .publish_gcs import publish_object


def execution_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", value):
        raise ValueError("An explicit Cloud Run execution name is required")
    return value


def read_json_object(remote, name, path, generation=None, checksum=None):
    blob = remote.get_blob(name, timeout=120)
    if blob is None:
        raise ValueError(f"Execution evidence is missing: {name}")
    if generation is not None and int(blob.generation) != int(generation):
        raise ValueError(f"Execution evidence generation changed: {name}")
    actual = download_verified(blob, path, checksum)
    return json.loads(Path(path).read_text(encoding="utf-8")), actual


def create_dbt_receipt(remote, root, result, project, bucket, execution, raw_dataset, location):
    artifacts = {}
    run_id = result["run_id"]
    for name, object_name in (("audit", f"runs/cloud_dbt/{run_id}.json"),
                              ("manifest", f"runs/dbt/{run_id}/manifest.json"),
                              ("results", f"runs/dbt/{run_id}/run_results.json")):
        path = root / f"dbt-{name}.json"
        _, generation = read_json_object(remote, object_name, path)
        artifacts[name] = {"object": object_name, "generation": generation, "sha256": sha256_file(path)}
    return {"schema_version": 1, "status": "success", "project": project, "bucket": bucket,
            "cloud_execution": execution_name(execution), "release_id": result["release_id"],
            "raw_dataset": raw_dataset, "location": location, "dbt_run_id": run_id,
            "analytics_dataset": result["analytics_dataset"], "result": result, "artifacts": artifacts}


def validate_dbt_receipt(remote, root, receipt, project, bucket, release_id, execution,
                         raw_dataset, dataset, location):
    expected = {"schema_version": 1, "status": "success", "project": project, "bucket": bucket,
                "release_id": release_id, "cloud_execution": execution_name(execution),
                "raw_dataset": raw_dataset, "analytics_dataset": dataset, "location": location}
    if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in expected.items()):
        raise ValueError("dbt receipt belongs to different execution inputs")
    run_id = receipt.get("dbt_run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("Invalid dbt run ID in execution receipt")
    paths = {"audit": f"runs/cloud_dbt/{run_id}.json", "manifest": f"runs/dbt/{run_id}/manifest.json",
             "results": f"runs/dbt/{run_id}/run_results.json"}
    target = root / "receipt-dbt"
    target.mkdir(exist_ok=True)
    documents = {}
    for name, object_name in paths.items():
        spec = receipt["artifacts"][name]
        if (spec.get("object") != object_name or type(spec.get("generation")) is not int
                or spec["generation"] <= 0 or not re.fullmatch(r"[a-f0-9]{64}", str(spec.get("sha256")))):
            raise ValueError("Invalid dbt receipt artifact binding")
        filename = {"audit": "audit.json", "manifest": "manifest.json", "results": "run_results.json"}[name]
        documents[name], _ = read_json_object(remote, object_name, target / filename,
                                             spec["generation"], spec["sha256"])
    summary = summarize_results(target, run_id, release_id, dataset)
    expected_result = {**summary, "audit_uri": f"gs://{bucket}/{paths['audit']}",
                       "artifacts_uri": f"gs://{bucket}/runs/dbt/{run_id}/", "cloud_execution": execution}
    audit = documents["audit"]
    if (receipt.get("result") != expected_result or audit.get("result") != expected_result
            or audit.get("status") != "success" or any(audit.get(k) != v for k, v in expected.items()
                                                       if k in ("project", "bucket", "release_id", "cloud_execution", "raw_dataset", "location"))):
        raise ValueError("dbt receipt and durable invocation evidence disagree")
    return expected_result


def publish_receipt(remote, root, stage, execution, receipt):
    execution_name(execution)
    path = root / f"{stage}-handoff.json"
    write_json(path, receipt)
    return publish_object(remote, {"file": str(path), "object_name": f"runs/handoffs/{stage}/{execution}.json",
                                   "sha256": sha256_file(path)})
