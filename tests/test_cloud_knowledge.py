"""Execute the knowledge worker against actual generated fixture releases and fake services."""

import json
from pathlib import Path

import pytest

from pbmc_platform import run_cloud_knowledge as worker
from pbmc_platform.knowledge_handoff import create_dbt_receipt, publish_receipt
from test_generate_knowledge import source_bundle
from test_validate_knowledge import validation_bundle


@pytest.fixture
def knowledge_run(validation_bundle, tmp_path, monkeypatch):
    args, _, reader = validation_bundle
    remote = args["storage_client"].remote
    result = json.loads(remote.objects[f"runs/cloud_dbt/{args['dbt_run_id']}.json"][0])["result"]
    root = tmp_path / "handoff-source"
    root.mkdir()
    receipt = create_dbt_receipt(remote, root, result, args["project"], args["bucket"],
                                 "fixture-execution", "pbmc_raw", "europe-west1")
    uploaded = publish_receipt(remote, root, "dbt", "fixture-execution", receipt)
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "pbmc-knowledge-fixture")
    return {**{key: args[key] for key in ("project", "bucket", "release_id", "dbt_run_id", "storage_client", "bigquery_client")},
            "dbt_execution": "fixture-execution", "dbt_handoff_generation": str(uploaded["generation"]),
            "work_dir": tmp_path / "worker"}


def test_worker_publishes_verified_output_and_reuses_completed_execution(knowledge_run):
    first = worker.run_cloud_knowledge(**knowledge_run)
    remote = knowledge_run["storage_client"].remote
    marker_name = first["publication_uri"].split(f"gs://{knowledge_run['bucket']}/")[1]
    original = remote.objects[marker_name]
    receipt_name = "runs/handoffs/knowledge/pbmc-knowledge-fixture.json"
    receipt = json.loads(remote.objects[receipt_name][0])
    assert receipt["dbt_execution"] == "fixture-execution"
    assert receipt["dbt_run_id"] == knowledge_run["dbt_run_id"]
    assert any(name.startswith("runs/knowledge_generation/") for name in remote.objects)
    assert any(name.startswith("runs/knowledge_validation/") for name in remote.objects)
    knowledge_run["bigquery_client"].list_rows = lambda *a, **k: pytest.fail("Completed execution must not regenerate")
    second = worker.run_cloud_knowledge(**knowledge_run)
    assert second == first
    assert remote.objects[marker_name] == original
    audit_records = [json.loads(data) for name, (data, _) in remote.objects.items() if name.startswith("runs/cloud_knowledge/")]
    assert sorted(a["reused"] for a in audit_records) == [False, True]


@pytest.mark.parametrize("fault", ["generation", "run", "release", "artifact"])
def test_invalid_dbt_handoff_stops_generation(knowledge_run, monkeypatch, fault):
    remote = knowledge_run["storage_client"].remote
    if fault == "generation":
        knowledge_run["dbt_handoff_generation"] = "999999"
    elif fault == "run":
        knowledge_run["dbt_run_id"] = "b" * 32
    else:
        name = "runs/handoffs/dbt/fixture-execution.json"
        payload, generation = remote.objects[name]
        receipt = json.loads(payload)
        if fault == "release":
            receipt["release_id"] = "0" * 64
        else:
            receipt["artifacts"]["manifest"]["sha256"] = "0" * 64
        remote.objects[name] = (json.dumps(receipt).encode(), generation)
    monkeypatch.setattr(worker, "run_generate", lambda *a, **k: pytest.fail("Invalid receipt reached generation"))
    with pytest.raises(ValueError):
        worker.run_cloud_knowledge(**knowledge_run)
    assert not any(name.startswith("knowledge/releases/") for name in remote.objects)
    assert "runs/handoffs/knowledge/pbmc-knowledge-fixture.json" not in remote.objects


def test_validation_failure_is_durable_and_never_published(knowledge_run):
    reader = knowledge_run["bigquery_client"]
    cohort = next(name for name in reader.rows if name.endswith("mart_cohort_cells"))
    reader.rows[cohort][0]["cell_id"] = "unexpected-cell"
    with pytest.raises(ValueError, match="Validation failed"):
        worker.run_cloud_knowledge(**knowledge_run)
    remote = knowledge_run["storage_client"].remote
    reports = [json.loads(data) for name, (data, _) in remote.objects.items() if name.startswith("runs/knowledge_validation/")]
    assert len(reports) == 1 and reports[0]["status"] == "failed"
    assert not any(name.startswith("knowledge/releases/") for name in remote.objects)
    assert "runs/handoffs/knowledge/pbmc-knowledge-fixture.json" not in remote.objects


@pytest.mark.parametrize("prefix", ["runs/cloud_knowledge/", "runs/handoffs/knowledge/"])
def test_receipt_waits_for_durable_audits_and_retry_preserves_prior_versions(knowledge_run, prefix):
    remote = knowledge_run["storage_client"].remote
    remote.fail_prefix = prefix
    with pytest.raises(RuntimeError, match="durable artifacts/handoff failed"):
        worker.run_cloud_knowledge(**knowledge_run)
    assert "runs/handoffs/knowledge/pbmc-knowledge-fixture.json" not in remote.objects
    previous = {name: value for name, value in remote.objects.items() if name.startswith("knowledge/releases/")}
    assert previous
    remote.fail_prefix = None
    assert worker.run_cloud_knowledge(**knowledge_run)["status"] == "published"
    assert all(remote.objects[name] == value for name, value in previous.items())
    assert "runs/handoffs/knowledge/pbmc-knowledge-fixture.json" in remote.objects
