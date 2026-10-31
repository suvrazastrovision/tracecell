import json
import shutil

import pytest

from storage_fakes import FakeClient

from pbmc_platform import run_cloud_qc as cloud
from pbmc_platform.publish_gcs import prepare_publish, publish_release


@pytest.fixture
def cloud_run(warehouse_release, tmp_path):
    _, processed = warehouse_release
    work = tmp_path / "cloud-work"
    shutil.copytree(processed.parent / "raw", work / "raw")
    kwargs = {
        "bucket": "pbmc-test-data", "project": "pbmc-okf-portfolio",
        "manifest": processed.parent / "dataset.yaml", "policy": processed.parent / "qc.yaml",
        "work_dir": work, "client": FakeClient(),
    }
    return kwargs, processed


def cloud_audits(client):
    return [json.loads(data) for name, (data, _) in client.remote.objects.items()
            if name.startswith("runs/cloud_qc/")]


def test_cloud_run_persists_raw_release_and_both_audits(cloud_run):
    kwargs, _ = cloud_run
    result = cloud.run_cloud_qc(**kwargs)
    remote = kwargs["client"].remote
    assert not result["qc_reused"]
    assert not result["restored_from_cloud"]
    assert result["summary"]["cells_total"] == 3
    assert any(name.startswith("raw/") for name in remote.objects)
    assert len([name for name in remote.objects if name.startswith("processed/releases/")]) == 5
    assert any(name.startswith("runs/qc/") for name in remote.objects)
    assert cloud_audits(kwargs["client"])[0]["status"] == "success"
    assert result["audit_uri"].endswith(f"/{result['run_id']}.json")


def test_fresh_worker_restores_without_https_or_qc_recomputation(cloud_run, monkeypatch, tmp_path):
    kwargs, _ = cloud_run
    first = cloud.run_cloud_qc(**kwargs)
    published = {name: value for name, value in kwargs["client"].remote.objects.items()
                 if name.startswith(("raw/", "processed/"))}

    def unexpected_download(*args, **kw):
        pytest.fail("A completed cloud run must not download the source over HTTPS")

    monkeypatch.setattr(cloud, "acquire_dataset", unexpected_download)
    kwargs["work_dir"] = tmp_path / "fresh-worker"
    second = cloud.run_cloud_qc(**kwargs)
    assert second["release_id"] == first["release_id"]
    assert second["qc_reused"] and second["restored_from_cloud"]
    assert second["publication"]["reused"]
    assert second["run_id"] != first["run_id"]
    assert all(kwargs["client"].remote.objects[name] == value for name, value in published.items())
    assert len(cloud_audits(kwargs["client"])) == 2


def test_existing_local_release_is_verified_and_reused(cloud_run):
    kwargs, _ = cloud_run
    cloud.run_cloud_qc(**kwargs)
    result = cloud.run_cloud_qc(**kwargs)
    assert result["qc_reused"] and result["publication"]["reused"]
    assert not result["restored_from_cloud"]


def test_corrupted_remote_raw_fails_without_https_fallback(cloud_run, monkeypatch):
    kwargs, _ = cloud_run
    dataset = cloud.read_config(kwargs["manifest"])
    name = f"raw/{dataset['sha256']}/{dataset['source_filename']}"
    kwargs["client"].remote.objects[name] = (b"corrupt", 1)

    def unexpected_download(*args, **kw):
        pytest.fail("Corrupted GCS input must not be hidden by an HTTPS fallback")

    monkeypatch.setattr(cloud, "acquire_dataset", unexpected_download)
    with pytest.raises(ValueError, match="Remote checksum mismatch"):
        cloud.run_cloud_qc(**kwargs)
    audit = cloud_audits(kwargs["client"])[0]
    assert audit["status"] == "failed"
    assert not any(name.startswith("processed/") for name in kwargs["client"].remote.objects)


@pytest.mark.parametrize("problem", ["missing", "corrupt", "identity"])
def test_invalid_committed_release_is_not_recomputed_or_published(cloud_run, problem):
    kwargs, processed = cloud_run
    plan = prepare_publish(processed, None, kwargs["bucket"], kwargs["project"])
    publish_release(plan, kwargs["client"])
    objects = kwargs["client"].remote.objects
    name = plan["objects"]["cells.parquet"]["object_name"]
    if problem == "missing":
        del objects[name]
    elif problem == "corrupt":
        objects[name] = (b"corrupt", 99)
    else:
        name = plan["objects"]["release.json"]["object_name"]
        metadata = json.loads(objects[name][0])
        metadata["identity"]["qc_policy"]["policy_version"] = "tampered"
        objects[name] = (json.dumps(metadata).encode(), 99)
    before = {name: data for name, data in objects.items() if name.startswith("processed/")}
    with pytest.raises(ValueError):
        cloud.run_cloud_qc(**kwargs)
    assert {name: data for name, data in objects.items() if name.startswith("processed/")} == before
    assert not (kwargs["work_dir"] / "processed" / "current.json").exists()
    assert not (kwargs["work_dir"] / "processed" / "releases" / plan["release_id"]).exists()
    assert cloud_audits(kwargs["client"])[0]["status"] == "failed"


def test_qc_failure_is_audited_in_gcs(cloud_run, monkeypatch):
    kwargs, _ = cloud_run

    def broken_qc(*args, **kw):
        raise ValueError("Injected QC failure")

    monkeypatch.setattr(cloud, "run_pipeline", broken_qc)
    with pytest.raises(ValueError, match="Injected QC failure"):
        cloud.run_cloud_qc(**kwargs)
    audit = cloud_audits(kwargs["client"])[0]
    assert audit["status"] == "failed" and "Injected QC failure" in audit["error"]


def test_audit_upload_failure_makes_successful_processing_fail(cloud_run, capsys):
    kwargs, _ = cloud_run
    kwargs["client"].remote.fail_prefix = "runs/"
    with pytest.raises(RuntimeError, match="durable audit upload failed"):
        cloud.run_cloud_qc(**kwargs)
    assert "cloud_qc_audit_upload_failed" in capsys.readouterr().err


def test_audit_upload_failure_preserves_original_qc_error(cloud_run, monkeypatch, capsys):
    kwargs, _ = cloud_run
    kwargs["client"].remote.fail_prefix = "runs/"

    def broken_qc(*args, **kw):
        raise ValueError("Original QC error")

    monkeypatch.setattr(cloud, "run_pipeline", broken_qc)
    with pytest.raises(ValueError, match="Original QC error"):
        cloud.run_cloud_qc(**kwargs)
    assert "cloud_qc_audit_upload_failed" in capsys.readouterr().err


def test_generation_change_during_download_fails(cloud_run, monkeypatch):
    kwargs, _ = cloud_run
    dataset = cloud.read_config(kwargs["manifest"])
    name = f"raw/{dataset['sha256']}/{dataset['source_filename']}"
    source = kwargs["work_dir"] / "raw" / dataset["source_filename"]
    remote = kwargs["client"].remote
    remote.objects[name] = (source.read_bytes(), 1)
    get_blob = remote.get_blob

    def changed_generation(object_name, **kw):
        blob = get_blob(object_name, **kw)
        if object_name == name:
            remote.objects[name] = (source.read_bytes(), 2)
        return blob

    monkeypatch.setattr(remote, "get_blob", changed_generation)
    from google.api_core.exceptions import PreconditionFailed

    with pytest.raises(PreconditionFailed, match="Generation changed"):
        cloud.run_cloud_qc(**kwargs)
    assert cloud_audits(kwargs["client"])[0]["status"] == "failed"
