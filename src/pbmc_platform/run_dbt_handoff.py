"""Run the existing dbt worker and commit a receipt bound to this Cloud Run execution."""

import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from .ingestion import canonical_json, sha256_file
from .knowledge_handoff import create_dbt_receipt, execution_name, publish_receipt, read_json_object, validate_dbt_receipt
from .load_warehouse import validate_identifier
from .publish_gcs import publish_object, validate_destination
from .run_cloud_dbt import run_cloud_dbt
from .run_qc import write_json
from datetime import datetime, timezone


def run_dbt_handoff(project, bucket, release_id, raw_dataset="pbmc_raw", location="europe-west1",
                    analytics_base="pbmc_analytics", work_dir=None, storage_client=None,
                    bigquery_client=None, runner=None):
    if work_dir is None:
        with tempfile.TemporaryDirectory(prefix="pbmc-dbt-handoff-") as temp:
            return run_dbt_handoff(project, bucket, release_id, raw_dataset, location, analytics_base,
                                   temp, storage_client, bigquery_client, runner)
    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    audit = {"run_id": uuid.uuid4().hex, "status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
             "project": project, "bucket": bucket, "release_id": release_id,
             "cloud_execution": os.environ.get("CLOUD_RUN_EXECUTION")}
    remote = None
    try:
        validate_destination(bucket, project)
        execution = execution_name(audit["cloud_execution"])
        for value in (raw_dataset, analytics_base):
            validate_identifier(value, "dataset")
        if not re.fullmatch(r"[a-f0-9]{64}", release_id):
            raise ValueError("Explicit full release ID required")
        if storage_client is None:
            from google.cloud import storage
            storage_client = storage.Client(project=project)
        remote = storage_client.bucket(bucket)
        name = f"runs/handoffs/dbt/{execution}.json"
        if remote.get_blob(name, timeout=120) is not None:
            receipt, _ = read_json_object(remote, name, root / "existing.json")
            result = validate_dbt_receipt(remote, root, receipt, project, bucket, release_id, execution,
                                           raw_dataset, f"{analytics_base}_r_{release_id}", location)
            audit["reused"] = True
        else:
            result = run_cloud_dbt(bucket, project, release_id, raw_dataset, location, analytics_base,
                                   work_dir=root / "build", storage_client=storage_client,
                                   bigquery_client=bigquery_client, runner=runner)
            receipt = create_dbt_receipt(remote, root, result, project, bucket, execution, raw_dataset, location)
            validate_dbt_receipt(remote, root, receipt, project, bucket, release_id, execution,
                                  raw_dataset, f"{analytics_base}_r_{release_id}", location)
            publish_receipt(remote, root, "dbt", execution, receipt)
            audit["reused"] = False
        audit.update(status="success", result=result)
        return result
    except Exception as error:
        audit.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        audit["finished_at"] = datetime.now(timezone.utc).isoformat()
        path = root / "handoff-audit.json"
        write_json(path, audit)
        print(canonical_json({"event": "dbt_handoff_run", **audit}), flush=True)
        if remote is not None:
            try:
                publish_object(remote, {"file": str(path), "object_name": f"runs/dbt_handoff/{audit['run_id']}.json",
                                        "sha256": sha256_file(path)})
            except Exception as error:
                print(canonical_json({"event": "dbt_handoff_audit_upload_failed", "run_id": audit["run_id"],
                                       "error": str(error)}), flush=True)
                if audit["status"] == "success":
                    raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("project", "bucket", "release-id"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--raw-dataset", default="pbmc_raw")
    parser.add_argument("--location", default="europe-west1")
    parser.add_argument("--analytics-base", default="pbmc_analytics")
    try:
        print(json.dumps(run_dbt_handoff(**vars(parser.parse_args())), indent=2))
    except Exception as error:
        parser.exit(1, f"dbt handoff failed: {error}\n")


if __name__ == "__main__":
    main()
