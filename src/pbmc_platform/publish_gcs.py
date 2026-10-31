"""Publish a validated local QC release to GCS without overwriting objects."""

import argparse
import json
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .ingestion import sha256_file
from .load_warehouse import prepare_load, validate_identifier
from .run_qc import OUTPUTS, write_json


def validate_destination(bucket, project):
    validate_identifier(project, "project")
    if not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]", bucket):
        raise ValueError("Expected a plain GCS bucket name, without gs://")


def prepare_publish(processed_dir, release_id, bucket, project):
    validate_destination(bucket, project)
    # Reuse the warehouse checks for identity, checksums, keys and QC reconciliation.
    validated = prepare_load(processed_dir, release_id)
    release_id = validated["release_id"]
    directory = Path(processed_dir).resolve() / "releases" / release_id
    metadata = json.loads((directory / "release.json").read_text(encoding="utf-8"))
    prefix = f"processed/releases/{release_id}"
    objects = {}
    # release.json is the completion marker and must always be published last.
    for name in (*OUTPUTS, "release.json"):
        path = directory / name
        objects[name] = {
            "file": str(path),
            "object_name": f"{prefix}/{name}",
            "sha256": sha256_file(path) if name == "release.json" else metadata["outputs"][name]["sha256"],
        }
    return {
        "project": project, "bucket": bucket, "release_id": release_id,
        "release_uri": f"gs://{bucket}/{prefix}", "objects": objects,
    }


def publish_object(bucket, spec):
    from google.api_core.exceptions import PreconditionFailed

    path = Path(spec["file"])
    if sha256_file(path) != spec["sha256"]:
        raise ValueError(f"Local file changed before upload: {path.name}")
    blob = bucket.blob(spec["object_name"])
    reused = False
    try:
        blob.upload_from_filename(
            str(path), if_generation_match=0, checksum="crc32c", timeout=120,
            content_type="application/json" if path.suffix == ".json" else "application/octet-stream",
        )
    except PreconditionFailed:
        # A prior attempt or concurrent writer created this object. Verify its
        # actual bytes rather than trusting editable custom checksum metadata.
        reused = True
        blob.reload(timeout=120)
    generation = int(blob.generation)
    with tempfile.TemporaryDirectory(prefix="gcs-verify-") as temp:
        downloaded = Path(temp) / "object"
        blob.download_to_filename(
            str(downloaded), if_generation_match=generation, checksum="crc32c", timeout=120,
        )
        if sha256_file(downloaded) != spec["sha256"]:
            raise ValueError(f"Remote checksum mismatch: {blob.name}; object was not overwritten")
    return {"generation": generation, "sha256": spec["sha256"], "reused": reused}


def publish_release(plan, client=None):
    from google.cloud import storage

    client = client if client is not None else storage.Client(project=plan["project"])
    bucket = client.bucket(plan["bucket"])
    results = {}
    # Do not rely on JSON/dict ordering if a plan was serialized by a caller.
    for name in (*OUTPUTS, "release.json"):
        results[name] = publish_object(bucket, plan["objects"][name])
    return {
        "release_id": plan["release_id"], "release_uri": plan["release_uri"],
        "manifest_uri": f"{plan['release_uri']}/release.json",
        "reused": all(item["reused"] for item in results.values()), "objects": results,
    }


def run_publish(bucket, project, processed_dir="data/processed", release_id=None,
                artifact_dir="artifacts", dry_run=False, client=None):
    runs_dir = Path(artifact_dir) / "storage_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    audit = {
        "run_id": uuid.uuid4().hex, "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running", "bucket": bucket, "project": project,
    }
    try:
        plan = prepare_publish(processed_dir, release_id, bucket, project)
        audit["release_id"] = plan["release_id"]
        result = plan if dry_run else publish_release(plan, client)
        audit.update(status="dry_run" if dry_run else "success", result=result)
        return result
    except Exception as error:
        audit.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        audit["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(runs_dir / f"{audit['run_id']}.json", audit)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--processed-dir", default="data/processed")
    parser.add_argument("--release-id")
    parser.add_argument("--artifact-dir", default="artifacts")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = run_publish(**vars(args))
    except Exception as error:
        parser.exit(1, f"GCS publication failed: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
