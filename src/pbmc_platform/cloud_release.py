"""Restore a committed GCS release without coupling it to the reader's runtime."""

import hashlib
import json
import re
import tempfile
from pathlib import Path

from .ingestion import canonical_json, sha256_file
from .load_warehouse import prepare_load
from .run_qc import OUTPUTS, validate_release


def download_verified(blob, destination, expected_sha256=None):
    generation = int(blob.generation)
    blob.download_to_filename(
        str(destination), if_generation_match=generation, checksum="crc32c", timeout=120,
    )
    if expected_sha256 is not None and sha256_file(destination) != expected_sha256:
        raise ValueError(f"Remote checksum mismatch: {blob.name}")
    return generation


def download_release(bucket, release_id, processed_dir, expected_identity=None, missing_ok=False):
    if not isinstance(release_id, str) or not re.fullmatch(r"[a-f0-9]{64}", release_id):
        raise ValueError("Expected a full SHA-256 release ID")
    root = Path(processed_dir)
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    destination = releases / release_id
    if destination.exists():
        validate_release(destination, expected_identity)
        prepare_load(root, release_id)
        return False
    prefix = f"processed/releases/{release_id}"
    marker = bucket.get_blob(f"{prefix}/release.json", timeout=120)
    if marker is None:
        if missing_ok:
            return False
        raise ValueError(f"Committed cloud release is missing: {release_id}")
    with tempfile.TemporaryDirectory(prefix="restore-", dir=releases) as temp:
        staging = Path(temp) / "releases" / release_id
        staging.mkdir(parents=True)
        download_verified(marker, staging / "release.json")
        metadata = json.loads((staging / "release.json").read_text(encoding="utf-8"))
        identity = metadata.get("identity")
        identity_hash = hashlib.sha256(canonical_json(identity).encode()).hexdigest()
        if (metadata.get("release_id") != release_id or identity_hash != release_id
                or (expected_identity is not None and identity != expected_identity)):
            raise ValueError("Cloud release identity does not match this run")
        for name in OUTPUTS:
            checksum = metadata["outputs"][name]["sha256"]
            if not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
                raise ValueError(f"Invalid cloud release checksum: {name}")
            blob = bucket.get_blob(f"{prefix}/{name}", timeout=120)
            if blob is None:
                raise ValueError(f"Committed cloud release is missing: {name}")
            download_verified(blob, staging / name, checksum)
        prepare_load(temp, release_id)
        staging.rename(destination)
    return True
