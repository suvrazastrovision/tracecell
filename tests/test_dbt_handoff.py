import json

import pytest

from pbmc_platform.run_dbt_handoff import run_dbt_handoff
from test_cloud_dbt import dbt_run


def test_receipt_binds_successful_dbt_and_retry_does_not_build_again(dbt_run, monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "pbmc-dbt-fixture")
    first = run_dbt_handoff(**dbt_run)
    remote = dbt_run["storage_client"].remote
    name = "runs/handoffs/dbt/pbmc-dbt-fixture.json"
    original = remote.objects[name]
    receipt = json.loads(original[0])
    assert receipt["dbt_run_id"] == first["run_id"]
    assert receipt["cloud_execution"] == first["cloud_execution"] == "pbmc-dbt-fixture"
    dbt_run["runner"] = lambda *a, **k: pytest.fail("Execution with a valid receipt must not rebuild")
    assert run_dbt_handoff(**dbt_run) == first
    assert remote.objects[name] == original


def test_failed_artifact_upload_cannot_create_receipt(dbt_run, monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "pbmc-dbt-fixture")
    dbt_run["storage_client"].remote.fail_prefix = "runs/cloud_dbt/"
    with pytest.raises(RuntimeError):
        run_dbt_handoff(**dbt_run)
    assert "runs/handoffs/dbt/pbmc-dbt-fixture.json" not in dbt_run["storage_client"].remote.objects


def test_changed_artifact_blocks_receipt_reuse(dbt_run, monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "pbmc-dbt-fixture")
    first = run_dbt_handoff(**dbt_run)
    remote = dbt_run["storage_client"].remote
    name = f"runs/dbt/{first['run_id']}/manifest.json"
    _, generation = remote.objects[name]
    remote.objects[name] = (b"changed", generation)
    dbt_run["runner"] = lambda *a, **k: pytest.fail("Conflicting receipt cannot be silently rebuilt")
    with pytest.raises(ValueError, match="checksum"):
        run_dbt_handoff(**dbt_run)
