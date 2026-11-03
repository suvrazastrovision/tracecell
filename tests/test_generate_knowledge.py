"""Exercise source selection, mapping and local output without cloud access."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

from pbmc_platform import generate_knowledge as knowledge
from pbmc_platform.ingestion import sha256_file
from pbmc_platform.load_warehouse import prepare_load
from pbmc_platform.publish_gcs import prepare_publish, publish_release
from pbmc_platform.run_qc import run_pipeline
from storage_fakes import FakeClient


@pytest.fixture
def source_bundle(warehouse_release, tmp_path):
    _, old_processed = warehouse_release
    source = next((old_processed / "releases").glob("*/release.json"))
    config = json.loads(source.read_text())["identity"]["dataset_manifest"]
    config.update(organism="Homo sapiens", tissue="blood", donor_count=1,
                  reference_genome="hg19", license="CC-BY-4.0", attribution="Fixture",
                  original_source="https://example.org/original")
    manifest = tmp_path / "knowledge-dataset.yaml"
    manifest.write_text(yaml.safe_dump(config))
    processed = tmp_path / "knowledge-processed"
    run_pipeline(manifest, tmp_path / "qc.yaml", tmp_path / "raw", processed, tmp_path / "audits")
    plan = prepare_load(processed)
    release_id = plan["release_id"]
    metadata_path = processed / "releases" / release_id / "release.json"
    metadata = json.loads(metadata_path.read_text())
    storage = FakeClient()
    publish_release(prepare_publish(processed, release_id, "pbmc-test-data", "pbmc-okf-portfolio"), storage)
    cells = pd.read_parquet(plan["tables"]["cells"]["file"])
    genes = pd.read_parquet(plan["tables"]["genes"]["file"])
    identity = {key: metadata["summary"][key] for key in ("dataset_id", "dataset_version", "policy_id", "policy_version")}
    metric = {**identity, "release_id": release_id,
              **{key: plan["summary"][key] for key in knowledge.METRICS
                 if key not in ("mitochondrial_features", "qc_pass_fraction")},
              "mitochondrial_features": int(genes.is_mitochondrial.sum()),
              "qc_pass_fraction": float(cells.qc_pass.mean())}
    exclusions = [{**identity, "release_id": release_id, "exclusion_reason": reason,
                   "excluded_cells": int(cells[reason].sum())} for reason in knowledge.REASONS]
    dataset = f"pbmc_analytics_r_{release_id}"
    run_id = "a" * 32
    invocation = "fixture-invocation"
    nodes = {"model.fixture.qc": {"resource_type": "model", "schema": dataset},
             "test.fixture.qc": {"resource_type": "test"}}
    manifest_data = {"metadata": {"invocation_id": invocation}, "nodes": nodes}
    results = {"metadata": {"invocation_id": invocation}, "results": [
        {"unique_id": "model.fixture.qc", "status": "success"},
        {"unique_id": "test.fixture.qc", "status": "pass"}]}
    audit = {"status": "success", "run_id": run_id, "project": "pbmc-okf-portfolio",
             "bucket": "pbmc-test-data", "release_id": release_id, "location": "europe-west1",
             "cloud_execution": "fixture-execution", "input_manifest": {
                 "uri": f"gs://pbmc-test-data/processed/releases/{release_id}/release.json",
                 "sha256": sha256_file(metadata_path)},
             "result": {"models": 1, "tests": 1, "dbt_invocation_id": invocation,
                        "run_id": run_id, "release_id": release_id, "analytics_dataset": dataset,
                        "audit_uri": f"gs://pbmc-test-data/runs/cloud_dbt/{run_id}.json",
                        "artifacts_uri": f"gs://pbmc-test-data/runs/dbt/{run_id}/",
                        "cloud_execution": "fixture-execution"}}
    objects = storage.remote.objects
    for name, value in [(f"runs/cloud_dbt/{run_id}.json", audit),
                        (f"runs/dbt/{run_id}/manifest.json", manifest_data),
                        (f"runs/dbt/{run_id}/run_results.json", results)]:
        objects[name] = (json.dumps(value).encode(), len(objects) + 1)

    class Reader:
        def get_dataset(self, name):
            assert name == f"pbmc-okf-portfolio.{dataset}"
            return SimpleNamespace(location="europe-west1")

        def list_rows(self, table, max_results):
            if table == f"pbmc-okf-portfolio.{dataset}.mart_dataset_qc":
                return [metric][:max_results]
            assert table == f"pbmc-okf-portfolio.{dataset}.mart_qc_exclusions"
            return exclusions[:max_results]

    return {"project": "pbmc-okf-portfolio", "bucket": "pbmc-test-data", "release_id": release_id,
            "dbt_run_id": run_id, "storage_client": storage, "bigquery_client": Reader(),
            "output_dir": tmp_path / "output"}, metric, exclusions


def test_cloud_sources_produce_deterministic_draft_and_report(source_bundle):
    kwargs, metric, _ = source_bundle
    storage = kwargs["storage_client"]
    writes_before = list(storage.remote.writes)
    result = knowledge.run_generate(**kwargs)
    package_path = Path(result["package_path"])
    package = json.loads(package_path.read_text())
    assert package["package_status"] == "draft"
    assert package["results"]["metrics"]["cells_total"] == metric["cells_total"] == 3
    assert package["results"]["metrics"]["cells_pass"] == 1
    assert package["dbt"]["models_succeeded"] == package["dbt"]["tests_passed"] == 1
    assert package["dataset"]["evidence_refs"] == ["release"]
    assert all(ref in package["evidence"] for ref in package["results"]["evidence_refs"])
    report_path = Path(result["report_path"])
    assert report_path.read_text() == knowledge.render_report(package)
    assert "fixture-invocation" in report_path.read_text()
    original_bytes, original_mtime = package_path.read_bytes(), package_path.stat().st_mtime_ns
    assert knowledge.run_generate(**kwargs) == result
    assert package_path.read_bytes() == original_bytes
    assert package_path.stat().st_mtime_ns == original_mtime
    assert storage.remote.writes == writes_before


@pytest.mark.parametrize("fault", ["failed_dbt", "wrong_invocation", "wrong_release",
                                   "duplicate_reason", "changed_metric", "missing_evidence",
                                   "manifest_mismatch", "failed_test", "corrupt_release"])
def test_bad_sources_fail_before_output(source_bundle, fault):
    kwargs, metric, exclusions = source_bundle
    objects = kwargs["storage_client"].remote.objects
    run_id = kwargs["dbt_run_id"]
    name = f"runs/cloud_dbt/{run_id}.json"
    if fault in ("failed_dbt", "manifest_mismatch"):
        audit = json.loads(objects[name][0])
        if fault == "failed_dbt":
            audit["status"] = "failed"
        else:
            audit["input_manifest"]["sha256"] = "0" * 64
        objects[name] = (json.dumps(audit).encode(), objects[name][1])
    elif fault in ("wrong_invocation", "failed_test"):
        name = f"runs/dbt/{run_id}/run_results.json"
        data = json.loads(objects[name][0])
        if fault == "wrong_invocation":
            data["metadata"]["invocation_id"] = "unrelated-run"
        else:
            data["results"][1]["status"] = "fail"
        objects[name] = (json.dumps(data).encode(), objects[name][1])
    elif fault == "wrong_release":
        metric["release_id"] = "0" * 64
    elif fault == "duplicate_reason":
        exclusions[1]["exclusion_reason"] = exclusions[0]["exclusion_reason"]
    elif fault == "changed_metric":
        metric["cells_pass"] += 1
    elif fault == "missing_evidence":
        del objects[name]
    else:
        name = f"processed/releases/{kwargs['release_id']}/cells.parquet"
        objects[name] = (b"changed bytes", objects[name][1])
    with pytest.raises(ValueError):
        knowledge.run_generate(**kwargs)
    assert not Path(kwargs["output_dir"]).exists()


def test_conflicting_output_is_preserved(source_bundle):
    kwargs, _, _ = source_bundle
    result = knowledge.run_generate(**kwargs)
    report = Path(result["report_path"])
    report.write_text("user modified report")
    with pytest.raises(FileExistsError, match="refusing overwrite"):
        knowledge.run_generate(**kwargs)
    assert report.read_text() == "user modified report"


def test_unsafe_invocation_path_is_rejected():
    example = json.loads((knowledge.SCHEMA_PATH.parents[1] / "examples/pbmc-knowledge-v1.json").read_text())
    example["dbt"]["invocation_id"] = "../escape"
    with pytest.raises(ValueError, match="Unsafe"):
        knowledge.write_package(example, "unused-output")


def test_report_uses_changed_package_values():
    example = json.loads((knowledge.SCHEMA_PATH.parents[1] / "examples/pbmc-knowledge-v1.json").read_text())
    changed = deepcopy(example)
    changed["results"]["metrics"]["cells_total"] = 42
    assert "| cells_total | 42 |" in knowledge.render_report(changed)
    assert "| cells_total | 2700 |" not in knowledge.render_report(changed)
