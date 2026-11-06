"""Private create-only objects with generation/checksum-pinned reads."""
import hashlib
import json
import os
from pathlib import Path


def sha(data):
    return hashlib.sha256(data).hexdigest()


class LocalBlobs:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key):
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ValueError("Invalid object path")
        return path

    def write(self, key, data):
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as f:
                f.write(data)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError("Conflicting object; existing bytes preserved")
        return {"key": key, "generation": sha(data), "sha256": sha(data), "size": len(data)}

    def read(self, key, generation=None):
        data = self.path(key).read_bytes()
        if generation is not None and sha(data) != str(generation):
            raise ValueError("Object generation changed")
        return data

    def metadata(self, key):
        data = self.read(key)
        return {"key": key, "generation": sha(data), "sha256": sha(data), "size": len(data)}

    def delete_prefix(self, prefix):
        # Only canonical app identifiers may reach this method.
        directory = self.path(prefix)
        if directory.exists():
            for path in directory.rglob("*"):
                if path.is_file():
                    path.unlink()


class GCSBlobs:
    def __init__(self, project, bucket):
        from google.cloud import storage
        self.client = storage.Client(project=project)
        self.bucket = self.client.bucket(bucket)

    def write(self, key, data):
        from google.api_core.exceptions import PreconditionFailed
        blob = self.bucket.blob(key)
        try:
            blob.upload_from_string(data, if_generation_match=0, checksum="crc32c")
        except PreconditionFailed:
            blob.reload()
            if blob.download_as_bytes(if_generation_match=int(blob.generation)) != data:
                raise ValueError("Conflicting object; existing bytes preserved")
        blob.reload()
        restored = blob.download_as_bytes(if_generation_match=int(blob.generation))
        if restored != data:
            raise ValueError("Object readback differs")
        return {"key": key, "generation": str(blob.generation), "sha256": sha(data), "size": len(data)}

    def read(self, key, generation=None):
        blob = self.bucket.blob(key, generation=int(generation) if generation else None)
        return blob.download_as_bytes(if_generation_match=int(generation) if generation else None)

    def metadata(self, key):
        blob = self.bucket.get_blob(key)
        if blob is None:
            raise ValueError("Upload has not completed")
        return {"key": key, "generation": str(blob.generation), "size": int(blob.size)}

    def upload_session(self, key, size, origin):
        return self.bucket.blob(key).create_resumable_upload_session(
            content_type="application/octet-stream", size=size, origin=origin, if_generation_match=0)

    def delete_prefix(self, prefix):
        for blob in self.client.list_blobs(self.bucket, prefix=prefix + "/"):
            blob.delete(if_generation_match=int(blob.generation))


def encode(value):
    return (json.dumps(value, sort_keys=True, allow_nan=False, indent=2) + "\n").encode()

