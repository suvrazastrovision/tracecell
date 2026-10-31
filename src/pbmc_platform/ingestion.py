"""Acquire only the checksum-pinned AnnData file declared by a manifest."""

import hashlib
import json
import re
import tempfile
from pathlib import Path
from urllib.request import urlopen

import yaml


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def read_config(path):
    with Path(path).open(encoding="utf-8-sig") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError(f"Expected a schema_version: 1 mapping in {path}")
    return value


def validate_manifest(manifest):
    for key in ("dataset_id", "dataset_version"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", str(manifest.get(key, ""))):
            raise ValueError(f"Invalid {key}")
    if not re.fullmatch(r"[a-f0-9]{64}", str(manifest.get("sha256", ""))):
        raise ValueError("sha256 must be a lowercase SHA-256 digest")
    filename = manifest.get("source_filename")
    if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.h5ad", filename):
        raise ValueError("source_filename must be a plain .h5ad filename")
    if not str(manifest.get("source_url", "")).startswith("https://"):
        raise ValueError("source_url must use HTTPS")
    for key in ("expected_cells", "expected_features"):
        if type(manifest.get(key)) is not int or manifest[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    if manifest.get("count_layer") != "X":
        raise ValueError("Only an unnormalized count matrix in X is supported")


def acquire_dataset(manifest, raw_dir):
    validate_manifest(manifest)
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    destination = raw_dir / manifest["source_filename"]
    if destination.exists():
        if sha256_file(destination) != manifest["sha256"]:
            raise ValueError(f"Checksum mismatch for cached input: {destination}")
        return destination

    # A failed or interrupted download never becomes the cached source file.
    with tempfile.TemporaryDirectory(prefix="download-", dir=raw_dir) as temp:
        candidate = Path(temp) / manifest["source_filename"]
        with urlopen(manifest["source_url"], timeout=60) as response:
            with candidate.open("wb") as handle:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    handle.write(chunk)
        if sha256_file(candidate) != manifest["sha256"]:
            raise ValueError("Downloaded input does not match the pinned SHA-256")
        candidate.replace(destination)
    return destination
