from types import SimpleNamespace

import pytest

from ops.knowledge_drill_worker import run, validate_isolation


@pytest.mark.parametrize("field,value", [("project", "other-project"), ("bucket", "pbmc-okf-portfolio-data"),
    ("raw_dataset", "pbmc_raw"), ("analytics_base", "pbmc_analytics"), ("scenario", "unknown")])
def test_knowledge_faults_reject_nonisolated_destinations(field, value):
    args = dict(project="pbmc-okf-portfolio", bucket="pbmc-okf-portfolio-drills", raw_dataset="pbmc_drill_raw",
                analytics_base="pbmc_drill_analytics", scenario="healthy")
    args[field] = value
    with pytest.raises(ValueError, match="isolated"):
        validate_isolation(**args)


def arguments():
    return dict(project="pbmc-okf-portfolio", bucket="pbmc-okf-portfolio-drills", release_id="a" * 64,
                dbt_run_id="b" * 32, dbt_execution="fixture-dbt", dbt_handoff_generation="1")


def environment(monkeypatch, scenario):
    monkeypatch.setenv("PBMC_KNOWLEDGE_DRILL_SCENARIO", scenario)
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "0")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "pbmc-drill-knowledge-fixture")


def test_publication_fault_preserves_first_file_boundary(monkeypatch):
    from pbmc_platform import publish_knowledge as publisher
    from pbmc_platform import run_cloud_knowledge as worker
    environment(monkeypatch, "publication_retry")
    committed = []
    monkeypatch.setattr(publisher, "publish_object", lambda remote, spec: committed.append(spec["object_name"]))

    def process(*args):
        for name in ("knowledge.json", "report.md"):
            publisher.publish_object(None, {"object_name": f"knowledge/releases/fixture/{name}"})

    monkeypatch.setattr(worker, "run_cloud_knowledge", process)
    with pytest.raises(RuntimeError, match="after knowledge.json committed"):
        run(**arguments())
    assert committed == ["knowledge/releases/fixture/knowledge.json"]


def test_completion_fault_happens_after_processing_returns(monkeypatch):
    from pbmc_platform import run_cloud_knowledge as worker
    environment(monkeypatch, "completion_retry")
    completed = []
    monkeypatch.setattr(worker, "run_cloud_knowledge", lambda *args: completed.append("receipt committed"))
    with pytest.raises(RuntimeError, match="after durable completion receipt"):
        run(**arguments())
    assert completed == ["receipt committed"]


@pytest.mark.parametrize("scenario", ["invalid_handoff", "validation_fail", "publication_retry", "completion_retry"])
def test_unrelated_infrastructure_errors_do_not_count_as_injected_failures(scenario):
    from ops.run_knowledge_drills import verify_expected_fault
    errors = [{"error": "403 PermissionDenied"}, {"error": "403 PermissionDenied"}]
    with pytest.raises(AssertionError):
        verify_expected_fault(scenario, errors, errors, [{"textPayload": "generic task failure"}])
