import json
from pathlib import Path

import pytest

from storage_fakes import FakeClient
from test_bigquery import FakeBigQuery

from pbmc_platform import run_cloud_warehouse as cloud
from pbmc_platform.publish_gcs import prepare_publish, publish_release


@pytest.fixture
def cloud_load(warehouse_release, tmp_path):
    plan, processed = warehouse_release
    storage = FakeClient()
    publication = prepare_publish(processed, plan["release_id"], "pbmc-test-data", "pbmc-okf-portfolio")
    publish_release(publication, storage)
    return {
        "bucket": "pbmc-test-data", "project": "pbmc-okf-portfolio",
        "release_id": plan["release_id"], "work_dir": tmp_path / "cloud-load",
        "storage_client": storage, "bigquery_client": FakeBigQuery(),
    }


def audits(kwargs):
    return [json.loads(data) for name, (data, _) in kwargs["storage_client"].remote.objects.items()
            if name.startswith("runs/cloud_warehouse/")]


def test_cloud_load_reads_committed_release_and_persists_audits(cloud_load, monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "warehouse-execution-fixture")
    result = cloud.run_cloud_load(**cloud_load)
    bq = cloud_load["bigquery_client"]
    assert result["release_id"] == cloud_load["release_id"]
    assert result["summary"]["cells_total"] == 3
    assert result["tables"]["genes"]["rows"] == 2
    assert not result["reused"]
    assert result["cloud_execution"] == "warehouse-execution-fixture"
    assert bq.dataset_creations == 0 and bq.dataset_reads == 1
    assert len(bq.loads) == 3
    saved = audits(cloud_load)[0]
    assert saved["status"] == "success" and saved["input_manifest"]["sha256"]
    assert any(name.startswith("runs/warehouse/") for name in cloud_load["storage_client"].remote.objects)


def test_fresh_worker_retry_does_not_reload_or_modify_release(cloud_load, tmp_path):
    first = cloud.run_cloud_load(**cloud_load)
    objects = cloud_load["storage_client"].remote.objects
    published = {name: value for name, value in objects.items() if name.startswith("processed/")}
    cloud_load["work_dir"] = tmp_path / "fresh-worker"
    second = cloud.run_cloud_load(**cloud_load)
    assert second["reused"] and first["release_id"] == second["release_id"]
    assert first["run_id"] != second["run_id"]
    assert len(cloud_load["bigquery_client"].loads) == 3
    assert all(objects[name] == value for name, value in published.items())
    assert len(audits(cloud_load)) == 2


def test_reader_does_not_recompute_producer_runtime_identity(cloud_load, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A warehouse reader must not recompute the producer's identity")

    monkeypatch.setattr("pbmc_platform.run_qc.build_identity", forbidden)
    assert cloud.run_cloud_load(**cloud_load)["release_id"] == cloud_load["release_id"]


@pytest.mark.parametrize("problem", ["missing_manifest", "corrupt_file", "wrong_identity"])
def test_unverified_cloud_release_never_reaches_bigquery(cloud_load, problem):
    objects = cloud_load["storage_client"].remote.objects
    prefix = f"processed/releases/{cloud_load['release_id']}"
    marker = f"{prefix}/release.json"
    if problem == "missing_manifest":
        del objects[marker]
    elif problem == "corrupt_file":
        objects[f"{prefix}/counts.h5ad"] = (b"corrupt", 42)
    else:
        metadata = json.loads(objects[marker][0])
        metadata["identity"]["runtime"]["python"] = "tampered"
        objects[marker] = (json.dumps(metadata).encode(), 42)
    with pytest.raises(ValueError):
        cloud.run_cloud_load(**cloud_load)
    assert cloud_load["bigquery_client"].loads == []
    assert audits(cloud_load)[0]["status"] == "failed"
    assert not (cloud_load["work_dir"] / "processed" / "releases" / cloud_load["release_id"]).exists()


@pytest.mark.parametrize("release_id", [None, "latest", "../../escape", "a" * 63])
def test_explicit_full_release_id_required_before_cloud_access(tmp_path, release_id):
    with pytest.raises(ValueError, match="full SHA-256"):
        cloud.run_cloud_load("pbmc-test-data", "pbmc-okf-portfolio", release_id,
                             work_dir=tmp_path, storage_client=object(), bigquery_client=object())
    saved = json.loads(next((tmp_path / "artifacts" / "cloud_warehouse_runs").glob("*.json")).read_text())
    assert saved["status"] == "failed"


def test_partial_load_failure_resumes_without_duplicate_rows(cloud_load, tmp_path):
    bq = cloud_load["bigquery_client"]
    bq.fail_once = "genes_r_"
    with pytest.raises(RuntimeError, match="Injected load"):
        cloud.run_cloud_load(**cloud_load)
    cloud_load["work_dir"] = tmp_path / "retry-worker"
    result = cloud.run_cloud_load(**cloud_load)
    assert result["tables"]["cells"]["reused"]
    assert not result["tables"]["genes"]["reused"]
    assert [item["status"] for item in audits(cloud_load)] == ["failed", "success"]
    assert len(bq.loads) == 4


def test_dataset_location_mismatch_prevents_load(cloud_load):
    cloud_load["bigquery_client"].location = "US"
    with pytest.raises(ValueError, match="different location"):
        cloud.run_cloud_load(**cloud_load)
    assert cloud_load["bigquery_client"].loads == []


def test_audit_failure_fails_an_otherwise_successful_load(cloud_load, capsys):
    cloud_load["storage_client"].remote.fail_prefix = "runs/"
    with pytest.raises(RuntimeError, match="durable audit upload failed"):
        cloud.run_cloud_load(**cloud_load)
    assert "cloud_warehouse_audit_upload_failed" in capsys.readouterr().err


def test_audit_failure_preserves_original_load_error(cloud_load, capsys):
    cloud_load["bigquery_client"].fail_once = "cells_r_"
    cloud_load["storage_client"].remote.fail_prefix = "runs/"
    with pytest.raises(RuntimeError, match="Injected load"):
        cloud.run_cloud_load(**cloud_load)
    assert "cloud_warehouse_audit_upload_failed" in capsys.readouterr().err
