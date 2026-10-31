"""GCS publication contract tests with an in-memory service; no credentials."""

import json
from pathlib import Path

import pytest

from storage_fakes import FakeClient

from pbmc_platform.publish_gcs import prepare_publish, publish_release, run_publish
from pbmc_platform.run_qc import OUTPUTS


@pytest.fixture
def publication(warehouse_release):
    _, processed = warehouse_release
    return prepare_publish(processed, None, "pbmc-test-data", "pbmc-okf-portfolio"), processed


def test_publication_verifies_bytes_and_commits_manifest_last(publication):
    plan, _ = publication
    client = FakeClient()
    result = publish_release(plan, client)
    assert not result["reused"]
    assert len(client.remote.writes) == 5
    assert client.remote.writes[-1].endswith("/release.json")
    for spec in plan["objects"].values():
        assert client.remote.objects[spec["object_name"]][0] == Path(spec["file"]).read_bytes()
    assert result["manifest_uri"].endswith(f"/{plan['release_id']}/release.json")


def test_replay_reuses_objects_without_creating_versions(publication):
    plan, _ = publication
    client = FakeClient()
    first = publish_release(plan, client)
    second = publish_release(plan, client)
    assert second["reused"]
    assert len(client.remote.writes) == 5
    assert {name: item["generation"] for name, item in first["objects"].items()} == {
        name: item["generation"] for name, item in second["objects"].items()
    }


def test_partial_failure_has_no_manifest_and_can_resume(publication):
    plan, _ = publication
    client = FakeClient()
    client.remote.fail_on = plan["objects"]["genes.parquet"]["object_name"]
    with pytest.raises(RuntimeError, match="Interrupted"):
        publish_release(plan, client)
    assert plan["objects"]["release.json"]["object_name"] not in client.remote.objects
    client.remote.fail_on = None
    result = publish_release(plan, client)
    assert result["objects"]["cells.parquet"]["reused"]
    assert len(client.remote.writes) == 5


def test_existing_conflicting_object_is_not_overwritten(publication):
    plan, _ = publication
    client = FakeClient()
    name = plan["objects"]["cells.parquet"]["object_name"]
    client.remote.objects[name] = (b"unexpected data", 42)
    with pytest.raises(ValueError, match="Remote checksum mismatch"):
        publish_release(plan, client)
    assert client.remote.objects[name] == (b"unexpected data", 42)
    assert not client.remote.writes


def test_corrupted_new_upload_prevents_manifest_publication(publication):
    plan, _ = publication
    client = FakeClient()
    client.remote.corrupt_on = plan["objects"]["counts.h5ad"]["object_name"]
    with pytest.raises(ValueError, match="Remote checksum mismatch"):
        publish_release(plan, client)
    assert plan["objects"]["release.json"]["object_name"] not in client.remote.objects


def test_manifest_conflict_is_detected(publication):
    plan, _ = publication
    client = FakeClient()
    publish_release(plan, client)
    name = plan["objects"]["release.json"]["object_name"]
    client.remote.objects[name] = (b"modified manifest", 99)
    with pytest.raises(ValueError, match="Remote checksum mismatch"):
        publish_release(plan, client)
    assert client.remote.objects[name] == (b"modified manifest", 99)


def test_changed_local_file_after_planning_is_rejected(publication):
    plan, _ = publication
    Path(plan["objects"]["cells.parquet"]["file"]).write_bytes(b"changed")
    client = FakeClient()
    with pytest.raises(ValueError, match="Local file changed"):
        publish_release(plan, client)
    assert not client.remote.writes


def test_dry_run_uses_no_client_and_writes_local_audit(publication, tmp_path):
    plan, processed = publication
    result = run_publish(
        "pbmc-test-data", "pbmc-okf-portfolio", processed, artifact_dir=tmp_path,
        dry_run=True, client=object(),
    )
    assert result == plan
    audit = json.loads(next((tmp_path / "storage_runs").glob("*.json")).read_text())
    assert audit["status"] == "dry_run"
    assert set(result["objects"]) == {*OUTPUTS, "release.json"}


def test_bad_local_release_is_rejected_before_client_access(publication, tmp_path):
    plan, processed = publication
    Path(plan["objects"]["counts.h5ad"]["file"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum mismatch"):
        run_publish("pbmc-test-data", "pbmc-okf-portfolio", processed, artifact_dir=tmp_path, client=object())
    audit = json.loads(next((tmp_path / "storage_runs").glob("*.json")).read_text())
    assert audit["status"] == "failed"


def test_failure_records_audit(publication, tmp_path):
    plan, processed = publication
    client = FakeClient()
    client.remote.fail_on = plan["objects"]["cells.parquet"]["object_name"]
    with pytest.raises(RuntimeError):
        run_publish("pbmc-test-data", "pbmc-okf-portfolio", processed, artifact_dir=tmp_path, client=client)
    audit = json.loads(next((tmp_path / "storage_runs").glob("*.json")).read_text())
    assert audit["status"] == "failed"
    assert "Interrupted upload" in audit["error"]


@pytest.mark.parametrize("bucket", ["gs://pbmc-test-data", "../invalid", "UPPERCASE"])
def test_invalid_bucket_is_rejected(warehouse_release, bucket):
    _, processed = warehouse_release
    with pytest.raises(ValueError, match="bucket name"):
        prepare_publish(processed, None, bucket, "pbmc-okf-portfolio")
