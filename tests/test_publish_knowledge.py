"""Publication protocol tests; validation audits here are trusted unit-test fixtures."""

import json
from pathlib import Path

import pytest

from pbmc_platform import publish_knowledge as publisher
from pbmc_platform.generate_knowledge import LIMITATIONS, SCHEMA_PATH
from storage_fakes import FakeClient


@pytest.fixture
def bundle(tmp_path):
    package = json.loads((SCHEMA_PATH.parents[1] / "examples/pbmc-knowledge-v1.json").read_text())
    package.update(package_status="verified", limitations=LIMITATIONS[1:])
    release_id = package["identity"]["release_id"]
    project, bucket, run_id = "pbmc-okf-portfolio", "pbmc-test-data", "a" * 32
    package["dataset"]["evidence_refs"] = package["qc_policy"]["evidence_refs"] = ["release"]
    package["dbt"]["invocation_id"] = "fixture-invocation"
    evidence = {
        "release": {"kind": "release_manifest", "uri": f"gs://{bucket}/processed/releases/{release_id}/release.json", "sha256": "b" * 64},
        "dbt_audit": {"kind": "dbt_audit", "uri": f"gs://{bucket}/runs/cloud_dbt/{run_id}.json", "sha256": "c" * 64},
        "dbt_manifest": {"kind": "dbt_manifest", "uri": f"gs://{bucket}/runs/dbt/{run_id}/manifest.json", "sha256": "d" * 64},
        "dbt_results": {"kind": "dbt_run_results", "uri": f"gs://{bucket}/runs/dbt/{run_id}/run_results.json", "sha256": "e" * 64},
        "dataset_qc": {"kind": "bigquery_table", "table_id": f"{project}.pbmc_analytics_r_{release_id}.mart_dataset_qc"},
        "qc_exclusions": {"kind": "bigquery_table", "table_id": f"{project}.pbmc_analytics_r_{release_id}.mart_qc_exclusions"},
    }
    package["evidence"] = evidence
    audit = {
        "status": "verified", "validation_id": "f" * 32, "project": project, "bucket": bucket,
        "release_id": release_id, "dbt_run_id": run_id, "analytics_base": "pbmc_analytics",
        "validator_sha256": publisher.sha256_file(publisher.validator.__file__),
        "schema_sha256": publisher.sha256_file(SCHEMA_PATH),
        "checks_passed": list(publisher.REQUIRED_CHECKS), "metrics": package["results"]["metrics"],
        "exclusions": package["results"]["exclusions"]["counts"], "cohort_rows": 2638,
        "dbt_invocation_id": package["dbt"]["invocation_id"],
        "objects": {source["uri"].split(f"gs://{bucket}/")[1]: {"sha256": source["sha256"], "generation": 1}
                    for source in evidence.values() if source["kind"] != "bigquery_table"},
        "tables": {source["table_id"]: {"etag": "fixture"} for source in evidence.values() if source["kind"] == "bigquery_table"},
    }
    directory = tmp_path / "verified"
    directory.mkdir()
    (directory / "knowledge.json").write_text(json.dumps(package))
    (directory / "report.md").write_bytes(publisher.render_report(package).encode())
    audit["outputs"] = {name: publisher.sha256_file(directory / name) for name in ("knowledge.json", "report.md")}
    (directory / "validation.json").write_text(json.dumps(audit))
    return {"validation_dir": directory, "project": project, "bucket": bucket, "release_id": release_id,
            "artifact_dir": tmp_path / "artifacts", "client": FakeClient()}


def plan_for(bundle, tmp_path):
    return publisher.prepare_bundle(bundle["validation_dir"], bundle["project"], bundle["bucket"],
                                    bundle["release_id"], tmp_path / "snapshot")


def rewrite(path, action):
    value = json.loads(path.read_text())
    action(value)
    path.write_text(json.dumps(value))


def test_publish_and_retry_preserve_bytes_generations_and_commit_last(bundle):
    remote = bundle["client"].remote
    first = publisher.run_publish_knowledge(**bundle)
    prefix = first["publication_uri"].split(f"gs://{bundle['bucket']}/")[1].rsplit("/", 1)[0]
    expected_order = [f"{prefix}/{name}" for name in (*publisher.BUNDLE_FILES, "publication.json")]
    assert remote.writes[:4] == expected_order
    assert not first["reused"]
    before = {name: remote.objects[name] for name in expected_order}
    second = publisher.run_publish_knowledge(**bundle)
    assert second["reused"]
    assert second["package_id"] == first["package_id"]
    assert {name: remote.objects[name] for name in expected_order} == before
    assert len(remote.objects) == 6  # four bundle objects plus two attempt audits
    marker = json.loads(remote.objects[expected_order[-1]][0])
    assert marker["package_id"] == publisher.digest(publisher.canonical_json(marker["identity"]).encode())
    for name in publisher.BUNDLE_FILES:
        payload, generation = remote.objects[f"{prefix}/{name}"]
        assert payload == (Path(bundle["validation_dir"]) / name).read_bytes()
        assert marker["objects"][name]["generation"] == generation
        assert marker["objects"][name]["sha256"] == publisher.digest(payload)
    audits = [json.loads(data) for name, (data, _) in remote.objects.items() if name.startswith("runs/knowledge_publication/")]
    assert len(audits) == 2 and all(audit["status"] == "success" for audit in audits)


@pytest.mark.parametrize("failed_file", ["knowledge.json", "report.md", "validation.json", "publication.json"])
def test_partial_failure_has_no_commit_and_retry_finishes_without_replacing_data(bundle, tmp_path, failed_file):
    remote = bundle["client"].remote
    plan = plan_for(bundle, tmp_path)
    prefix = plan["prefix"]
    remote.fail_on = f"{prefix}/{failed_file}"
    with pytest.raises(RuntimeError, match="Interrupted upload"):
        publisher.run_publish_knowledge(**bundle)
    assert f"{prefix}/publication.json" not in remote.objects
    completed = {name: value for name, value in remote.objects.items() if name.startswith(prefix)}
    remote.fail_on = None
    result = publisher.run_publish_knowledge(**bundle)
    assert result["status"] == "published"
    assert all(remote.objects[name] == value for name, value in completed.items())
    assert f"{prefix}/publication.json" in remote.objects


@pytest.mark.parametrize("filename", ["report.md", "publication.json"])
def test_existing_conflicting_object_is_never_overwritten_or_committed(bundle, tmp_path, filename):
    plan = plan_for(bundle, tmp_path)
    remote = bundle["client"].remote
    name = f"{plan['prefix']}/{filename}"
    remote.objects[name] = (b"conflicting content", 99)
    with pytest.raises(RuntimeError, match="not overwritten"):
        publisher.run_publish_knowledge(**bundle)
    assert remote.objects[name] == (b"conflicting content", 99)
    if filename != "publication.json":
        assert f"{plan['prefix']}/publication.json" not in remote.objects


@pytest.mark.parametrize("fault", ["draft", "failed_validation", "changed_report", "missing_check", "wrong_project",
                                   "old_validator", "old_schema", "changed_metric", "missing_evidence_generation"])
def test_invalid_local_bundle_fails_before_upload(bundle, fault):
    directory = Path(bundle["validation_dir"])
    if fault == "draft":
        rewrite(directory / "knowledge.json", lambda p: p.update(package_status="draft"))
    elif fault == "changed_report":
        (directory / "report.md").write_text("changed report")
    else:
        changes = {
            "failed_validation": lambda a: a.update(status="failed"),
            "missing_check": lambda a: a["checks_passed"].pop(),
            "wrong_project": lambda a: a.update(project="another-project"),
            "old_validator": lambda a: a.update(validator_sha256="0" * 64),
            "old_schema": lambda a: a.update(schema_sha256="0" * 64),
            "changed_metric": lambda a: a["metrics"].update(cells_total=999),
            "missing_evidence_generation": lambda a: next(iter(a["objects"].values())).pop("generation"),
        }
        rewrite(directory / "validation.json", changes[fault])
    with pytest.raises(RuntimeError):
        publisher.run_publish_knowledge(**bundle)
    assert not bundle["client"].remote.objects
    audit_path = next((Path(bundle["artifact_dir"]) / "knowledge_publication_runs").glob("*.json"))
    assert json.loads(audit_path.read_text())["status"] == "failed"


def test_dry_run_checks_bundle_without_cloud_calls(bundle):
    class NoCloud:
        def bucket(self, name):
            raise AssertionError("Dry run must not access cloud storage")

    bundle["client"] = NoCloud()
    result = publisher.run_publish_knowledge(**bundle, dry_run=True)
    assert result["publication_uri"].endswith("/publication.json")
    assert result["audit_object"] is None


def test_new_validation_creates_a_separate_version(bundle):
    first = publisher.run_publish_knowledge(**bundle)
    snapshot = dict(bundle["client"].remote.objects)
    rewrite(Path(bundle["validation_dir"]) / "validation.json", lambda a: a.update(validation_id="1" * 32))
    second = publisher.run_publish_knowledge(**bundle)
    assert first["package_id"] != second["package_id"]
    assert all(bundle["client"].remote.objects[name] == value for name, value in snapshot.items())


def test_upload_uses_checked_snapshot_even_if_original_changes(bundle, monkeypatch):
    original = publisher.publish_object
    report = Path(bundle["validation_dir"]) / "report.md"
    expected = report.read_bytes()

    def changed(remote, spec):
        report.write_text("changed after snapshot")
        return original(remote, spec)

    monkeypatch.setattr(publisher, "publish_object", changed)
    result = publisher.run_publish_knowledge(**bundle)
    name = result["objects"]["report.md"]["uri"].split(f"gs://{bundle['bucket']}/")[1]
    assert bundle["client"].remote.objects[name][0] == expected


def test_audit_upload_failure_reports_failure_and_retry_reuses_committed_bundle(bundle):
    remote = bundle["client"].remote
    remote.fail_prefix = "runs/knowledge_publication/"
    with pytest.raises(RuntimeError, match="publication audit upload failed"):
        publisher.run_publish_knowledge(**bundle)
    assert any(name.endswith("publication.json") for name in remote.objects)
    remote.fail_prefix = None
    assert publisher.run_publish_knowledge(**bundle)["reused"]


def test_publisher_accepts_actual_validator_output(tmp_path, warehouse_release):
    # Run the real fixture-based generation and validation path, rather than
    # relying only on the trusted synthetic audit used by the protocol unit tests.
    from test_generate_knowledge import source_bundle
    from test_validate_knowledge import validation_bundle

    sources = source_bundle.__wrapped__(warehouse_release, tmp_path)
    validation_args, _, _ = validation_bundle.__wrapped__(sources, tmp_path)
    validated = publisher.validator.run_validate(**validation_args)
    result = publisher.run_publish_knowledge(
        validation_dir=Path(validated["package_path"]).parent,
        project=validation_args["project"], bucket=validation_args["bucket"],
        release_id=validation_args["release_id"], artifact_dir=tmp_path / "publisher-audits",
        client=validation_args["storage_client"])
    assert result["status"] == "published"
