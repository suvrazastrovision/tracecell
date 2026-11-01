from types import SimpleNamespace
import pytest
from ops.drill_worker import fail_second_table, prepare_dbt_project, validate_isolation
from ops.run_drills import read_logs


@pytest.mark.parametrize("field,value", [("project", "other-project"), ("bucket", "pbmc-okf-portfolio-data"),
    ("raw_dataset", "pbmc_raw"), ("analytics_base", "pbmc_analytics"), ("stage", "other"), ("scenario", "other")])
def test_fault_injection_rejects_nonisolated_resources(field, value):
    args = dict(project="pbmc-okf-portfolio", bucket="pbmc-okf-portfolio-drills", raw_dataset="pbmc_drill_raw",
                analytics_base="pbmc_drill_analytics", stage="warehouse", scenario="load_retry")
    args[field] = value
    with pytest.raises(ValueError, match="isolated"):
        validate_isolation(**args)


def test_partial_load_fault_preserves_first_commit_and_recovers():
    committed = []
    client = SimpleNamespace(create_table=lambda name: committed.append(name))
    fail_second_table(client)
    client.create_table("cells")
    with pytest.raises(RuntimeError, match="first table committed"):
        client.create_table("genes")
    assert committed == ["cells"]
    client.create_table("genes")
    assert committed == ["cells", "genes"]


def test_log_search_continues_past_empty_pages():
    calls = []
    pages = iter([{"nextPageToken": "continue-search"}, {"entries": [{"jsonPayload": {"attempt": 0}}], "nextPageToken": "second"},
                  {"entries": [{"jsonPayload": {"attempt": 1}}]}])
    def api(method, url, **kwargs):
        calls.append(kwargs["json"].copy())
        return next(pages)
    assert [entry["jsonPayload"]["attempt"] for entry in read_logs(api, 'timestamp>="2026-11-01T00:00:00Z"')] == [0, 1]
    assert [call.get("pageToken") for call in calls] == [None, "continue-search", "second"]
    assert len({call["filter"] for call in calls}) == 1


def test_injected_test_uses_writable_private_copy_of_readonly_project(tmp_path):
    source = tmp_path / "source"
    (source / "tests").mkdir(parents=True)
    (source / "tests").chmod(0o555)
    try:
        target = prepare_dbt_project(tmp_path / "copy", True, source)
        assert (target / "tests" / "drill_forced_failure.sql").read_text() == "select 1 as injected_failure\n"
        assert not (source / "tests" / "drill_forced_failure.sql").exists()
    finally:
        (source / "tests").chmod(0o700)
