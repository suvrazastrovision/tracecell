"""Load an explicitly selected, verified GCS release into an existing BigQuery dataset."""

import argparse
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .cloud_release import download_release
from .ingestion import canonical_json, sha256_file
from .load_warehouse import run_load, validate_identifier
from .publish_gcs import publish_object, validate_destination
from .run_qc import write_json


def run_cloud_load(bucket, project, release_id, raw_dataset="pbmc_raw", location="europe-west1",
                   work_dir=None, storage_client=None, bigquery_client=None):
    if work_dir is None:
        with tempfile.TemporaryDirectory(prefix="pbmc-cloud-warehouse-") as temp:
            return run_cloud_load(bucket, project, release_id, raw_dataset, location, temp,
                                  storage_client, bigquery_client)
    root = Path(work_dir).resolve()
    artifacts = root / "artifacts"
    audit_dir = artifacts / "cloud_warehouse_runs"
    audit_dir.mkdir(parents=True, exist_ok=True)
    prior_audits = set((artifacts / "warehouse_runs").glob("*.json"))
    audit = {
        "run_id": uuid.uuid4().hex, "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running", "bucket": bucket, "project": project, "release_id": release_id,
        "raw_dataset": raw_dataset, "location": location,
        "cloud_execution": os.environ.get("CLOUD_RUN_EXECUTION"),
    }
    remote = None
    try:
        validate_destination(bucket, project)
        validate_identifier(raw_dataset, "dataset")
        if not isinstance(release_id, str) or not re.fullmatch(r"[a-f0-9]{64}", release_id):
            raise ValueError("Expected a full SHA-256 release ID; never use an implicit latest release")
        if not re.fullmatch(r"[a-zA-Z0-9-]+", location):
            raise ValueError("Invalid BigQuery location")
        if storage_client is None:
            from google.cloud import storage

            storage_client = storage.Client(project=project)
        remote = storage_client.bucket(bucket)
        audit["downloaded_from_cloud"] = download_release(remote, release_id, root / "processed")
        manifest = root / "processed" / "releases" / release_id / "release.json"
        audit["input_manifest"] = {
            "uri": f"gs://{bucket}/processed/releases/{release_id}/release.json",
            "sha256": sha256_file(manifest),
        }
        loaded = run_load(
            target="bigquery", project=project, processed_dir=root / "processed", release_id=release_id,
            raw_dataset=raw_dataset, location=location, artifact_dir=artifacts,
            client=bigquery_client, require_existing_dataset=True,
        )
        result = {
            **loaded, "run_id": audit["run_id"], "cloud_execution": audit["cloud_execution"],
            "reused": all(item["reused"] for item in loaded["tables"].values()),
            "audit_uri": f"gs://{bucket}/runs/cloud_warehouse/{audit['run_id']}.json",
        }
        audit.update(status="success", result=result)
        return result
    except Exception as error:
        audit.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        audit["finished_at"] = datetime.now(timezone.utc).isoformat()
        path = audit_dir / f"{audit['run_id']}.json"
        write_json(path, audit)
        print(canonical_json({"event": "cloud_warehouse_run", **audit}), flush=True)
        failures = []
        if remote is not None:
            new_audits = sorted(set((artifacts / "warehouse_runs").glob("*.json")) - prior_audits)
            records = [(item, f"runs/warehouse/{item.name}") for item in new_audits]
            records.append((path, f"runs/cloud_warehouse/{path.name}"))
            for local, name in records:
                try:
                    publish_object(remote, {
                        "file": str(local), "object_name": name, "sha256": sha256_file(local),
                    })
                except Exception as error:
                    failures.append(f"{type(error).__name__}: {error}")
                    print(canonical_json({
                        "event": "cloud_warehouse_audit_upload_failed", "run_id": audit["run_id"],
                        "object_name": name, "error": failures[-1],
                    }), file=sys.stderr, flush=True)
        if failures and audit["status"] == "success":
            raise RuntimeError("Warehouse load completed, but durable audit upload failed: " + "; ".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--raw-dataset", default="pbmc_raw")
    parser.add_argument("--location", default="europe-west1")
    parser.add_argument("--work-dir", help="Optional local directory; one writer only")
    args = parser.parse_args()
    try:
        result = run_cloud_load(**vars(args))
    except Exception as error:
        parser.exit(1, f"Cloud warehouse load failed: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
