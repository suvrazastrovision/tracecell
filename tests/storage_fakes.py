"""Small in-memory GCS service used by storage and cloud orchestration tests."""

from pathlib import Path

from google.api_core.exceptions import PreconditionFailed


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name, self.generation = bucket, name, None

    def upload_from_filename(self, filename, *, if_generation_match, checksum, timeout, content_type):
        assert if_generation_match == 0 and checksum == "crc32c"
        if self.name == self.bucket.fail_on or self.name.startswith(self.bucket.fail_prefix or "\0"):
            raise RuntimeError("Interrupted upload")
        if self.name in self.bucket.objects:
            raise PreconditionFailed("Object already exists")
        data = Path(filename).read_bytes()
        if self.name == self.bucket.corrupt_on:
            data = b"corrupted"
        self.generation = len(self.bucket.objects) + 1
        self.bucket.objects[self.name] = (data, self.generation)
        self.bucket.writes.append(self.name)

    def reload(self, *, timeout):
        self.generation = self.bucket.objects[self.name][1]

    def download_to_filename(self, filename, *, if_generation_match, checksum, timeout):
        data, generation = self.bucket.objects[self.name]
        assert checksum == "crc32c"
        if if_generation_match != generation:
            raise PreconditionFailed("Generation changed")
        Path(filename).write_bytes(data)


class FakeBucket:
    def __init__(self):
        self.objects, self.writes = {}, []
        self.fail_on = self.corrupt_on = self.fail_prefix = None

    def blob(self, name):
        return FakeBlob(self, name)

    def get_blob(self, name, *, timeout):
        if name not in self.objects:
            return None
        blob = self.blob(name)
        blob.reload(timeout=timeout)
        return blob


class FakeClient:
    def __init__(self):
        self.remote = FakeBucket()

    def bucket(self, name):
        assert name == "pbmc-test-data"
        return self.remote
