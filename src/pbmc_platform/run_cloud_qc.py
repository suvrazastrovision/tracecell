"""Run QC with GCS-backed inputs, verified release reuse and durable audits."""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .ingestion import acquire_dataset, canonical_json, read_config, sha256_file, validate_manifest
from .cloud_release import download_release, download_verified
from .publish_gcs import prepare_publish, publish_object, publish_release, validate_destination
from .qc import validate_policy
from .run_qc import build_identity, run_pipeline, write_json


def ensure_raw(bucket, bucket_name, manifest, raw_dir):
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / manifest["source_filename"]
    if path.exists() and sha256_file(path) != manifest["sha256"]:
        raise ValueError(f"Checksum mismatch for cached input: {path}")
    name = f"raw/{manifest['sha256']}/{manifest['source_filename']}"
    blob = bucket.get_blob(name, timeout=120)
    if blob is None:
        source = acquire_dataset(manifest, raw_dir)
        receipt = publish_object(bucket, {
            "file": str(source), "object_name": name, "sha256": manifest["sha256"],
        })
    else:
        with tempfile.TemporaryDirectory(prefix="raw-download-", dir=raw_dir) as temp:
            candidate = Path(temp) / "source.h5ad"
            generation = download_verified(blob, candidate, manifest["sha256"])
            candidate.replace(path)
        receipt = {"generation": generation, "sha256": manifest["sha256"], "reused": True}
    return {"uri": f"gs://{bucket_name}/{name}", **receipt}


def restore_release(bucket, identity, processed_dir):
    release_id = hashlib.sha256(canonical_json(identity).encode()).hexdigest()
    return download_release(bucket, release_id, processed_dir, expected_identity=identity, missing_ok=True)


def publish_handoff(remote, root, result, execution):
    """Commit a deterministic receipt only after all processing audits are durable.

    The execution name selects this receipt; no mutable latest pointer is used.
    Content excludes per-attempt UUIDs so a successful task retry can reuse it.
    """
    receipt = {
        "schema_version": 1, "status": "success", "cloud_execution": execution,
        "release_id": result["release_id"],
        "manifest_uri": result["publication"]["manifest_uri"],
        "manifest_sha256": result["publication"]["objects"]["release.json"]["sha256"],
    }
    path = root / "artifacts" / "handoff.json"
    write_json(path, receipt)
    return publish_object(remote, {
        "file": str(path), "object_name": f"runs/handoffs/qc/{execution}.json",
        "sha256": sha256_file(path),
    })


def run_cloud_qc(bucket, project, manifest="configs/dataset.yaml", policy="configs/qc.yaml",
                 work_dir=None, client=None):
    if work_dir is None:
        with tempfile.TemporaryDirectory(prefix="pbmc-cloud-qc-") as temp:
            return run_cloud_qc(bucket, project, manifest, policy, temp, client)

    root = Path(work_dir).resolve()
    artifact_dir = root / "artifacts"
    audit_dir = artifact_dir / "cloud_runs"
    audit_dir.mkdir(parents=True, exist_ok=True)
    prior_qc_audits = set((artifact_dir / "runs").glob("*.json"))
    audit = {
        "run_id": uuid.uuid4().hex, "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running", "bucket": bucket, "project": project,
        "cloud_execution": os.environ.get("CLOUD_RUN_EXECUTION"),
    }
    remote = None
    try:
        validate_destination(bucket, project)
        if audit["cloud_execution"] is not None and not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", audit["cloud_execution"]):
            raise ValueError("Invalid Cloud Run execution name")
        if client is None:
            from google.cloud import storage

            client = storage.Client(project=project)
        remote = client.bucket(bucket)
        dataset, qc_policy = read_config(manifest), read_config(policy)
        validate_manifest(dataset)
        validate_policy(qc_policy)
        identity = build_identity(dataset, qc_policy)
        audit["release_id"] = hashlib.sha256(canonical_json(identity).encode()).hexdigest()
        audit["source"] = ensure_raw(remote, bucket, dataset, root / "raw")
        audit["restored_from_cloud"] = restore_release(remote, identity, root / "processed")
        qc = run_pipeline(manifest, policy, root / "raw", root / "processed", artifact_dir)
        audit["qc"] = qc
        plan = prepare_publish(root / "processed", qc["release_id"], bucket, project)
        publication = publish_release(plan, client)
        result = {
            "run_id": audit["run_id"], "release_id": qc["release_id"], "summary": qc["summary"],
            "qc_reused": qc["reused"], "restored_from_cloud": audit["restored_from_cloud"],
            "publication": publication,
            "audit_uri": f"gs://{bucket}/runs/cloud_qc/{audit['run_id']}.json",
            "cloud_execution": audit["cloud_execution"],
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
        print(canonical_json({"event": "cloud_qc_run", **audit}), flush=True)
        failures = []
        if remote is not None:
            # Attempt the outer audit even if a detailed QC audit cannot be
            # uploaded. Preserve the original processing exception on failure.
            new_qc_audits = sorted(set((artifact_dir / "runs").glob("*.json")) - prior_qc_audits)
            records = [(item, f"runs/qc/{item.name}") for item in new_qc_audits]
            records.append((path, f"runs/cloud_qc/{path.name}"))
            for local, name in records:
                try:
                    publish_object(remote, {
                        "file": str(local), "object_name": name, "sha256": sha256_file(local),
                    })
                except Exception as error:
                    failures.append(f"{type(error).__name__}: {error}")
                    print(canonical_json({
                        "event": "cloud_qc_audit_upload_failed", "run_id": audit["run_id"],
                        "object_name": name, "error": failures[-1],
                    }), file=sys.stderr, flush=True)
            if not failures and audit["status"] == "success" and audit["cloud_execution"]:
                try:
                    publish_handoff(remote, root, audit["result"], audit["cloud_execution"])
                except Exception as error:
                    failures.append(f"{type(error).__name__}: {error}")
                    print(canonical_json({
                        "event": "cloud_qc_handoff_upload_failed", "run_id": audit["run_id"],
                        "cloud_execution": audit["cloud_execution"], "error": failures[-1],
                    }), file=sys.stderr, flush=True)
        if failures and audit["status"] == "success":
            raise RuntimeError("QC release published, but durable audit upload failed: " + "; ".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--manifest", default="configs/dataset.yaml")
    parser.add_argument("--policy", default="configs/qc.yaml")
    parser.add_argument("--work-dir", help="Optional persistent local work directory; one writer only")
    args = parser.parse_args()
    try:
        result = run_cloud_qc(**vars(args))
    except Exception as error:
        parser.exit(1, f"Cloud QC pipeline failed: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
