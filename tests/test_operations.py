from datetime import date
import json
from types import SimpleNamespace

import pytest

from pbmc_platform.recover_pipeline_lock import release_retained_lock
from pbmc_platform.report_costs import query_for, run_report, summarize


OWNER = "00000000-0000-0000-0000-000000000001"


def recovery_fixture(state="FAILED", active_job=False, generation=5, changed_owner=False):
    deleted = []
    lock = {"schema_version": 1, "owner": OWNER if not changed_owner else "another-owner", "workflow": "pbmc-qc-workflow"}

    def download(**kwargs):
        assert kwargs["if_generation_match"] == generation
        return json.dumps(lock).encode()

    def delete(**kwargs):
        deleted.append(kwargs["if_generation_match"])

    blob = SimpleNamespace(generation=generation, download_as_bytes=download, delete=delete)
    client = SimpleNamespace(bucket=lambda _: SimpleNamespace(get_blob=lambda *args, **kwargs: blob))

    def api(url):
        if "workflowexecutions" in url:
            return {"state": state}
        if active_job:
            return {"executions": [{"name": "still-running"}]}
        return {"executions": [{"name": "finished", "completionTime": "2026-11-01T00:00:00Z"}]}

    return client, api, deleted


@pytest.mark.parametrize("fault", ["active_owner", "running_job", "changed_generation", "changed_owner"])
def test_lock_recovery_rejects_unsafe_release(fault):
    client, api, deleted = recovery_fixture(state="ACTIVE" if fault == "active_owner" else "FAILED",
        active_job=fault == "running_job", generation=6 if fault == "changed_generation" else 5,
        changed_owner=fault == "changed_owner")
    with pytest.raises(ValueError):
        release_retained_lock("production", OWNER, 5, client, api)
    assert deleted == []


def test_cancelled_workflow_does_not_make_active_cloud_run_task_safe():
    client, api, deleted = recovery_fixture(state="CANCELLED", active_job=True)
    with pytest.raises(ValueError, match="still be active"):
        release_retained_lock("production", OWNER, 5, client, api)
    assert not deleted


def test_terminal_owner_and_completed_jobs_allow_generation_bound_recovery():
    client, api, deleted = recovery_fixture()
    result = release_retained_lock("production", OWNER, 5, client, api)
    assert result["status"] == "released" and deleted == [5]
    assert result["completed_job_executions_checked"] == 4


def test_lock_recovery_checks_job_execution_continuation_pages():
    client, base, deleted = recovery_fixture()

    def api(url):
        if "workflowexecutions" in url:
            return base(url)
        if "pageToken=" not in url:
            return {"executions": [], "nextPageToken": "page two"}
        return {"executions": [{"name": "active-on-next-page"}]}

    with pytest.raises(ValueError, match="active-on-next-page"):
        release_retained_lock("production", OWNER, 5, client, api)
    assert deleted == []


def test_cost_totals_keep_credits_and_currencies_separate():
    rows, totals = summarize([
        {"usage_date": date(2026, 11, 1), "service": "Cloud Run", "currency": "EUR", "gross_cost": "2.50", "credits": "-1.20", "net_cost": "1.30"},
        {"usage_date": date(2026, 11, 1), "service": "Storage", "currency": "EUR", "gross_cost": ".20", "credits": "0", "net_cost": ".20"},
        {"usage_date": date(2026, 11, 1), "service": "Cloud Run", "currency": "USD", "gross_cost": "1", "credits": "-.50", "net_cost": ".50"},
    ])
    assert totals["EUR"]["net_cost"] == "1.50" and totals["USD"]["net_cost"] == "0.50"
    assert rows[0]["credits"] == "-1.20"


def test_missing_export_and_no_rows_never_claim_zero_actual_cost():
    report = run_report(None, date(2026, 11, 1), date(2026, 12, 1))
    assert report["status"] == "billing_export_not_configured" and report["totals_by_currency"] is None


def test_empty_export_period_is_not_a_zero_cost_claim():
    calls = []

    def query(sql, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(total_bytes_processed=10, job_id="fixture-cost-job", result=lambda: [])

    report = run_report("pbmc-okf-portfolio.pbmc_billing_export.gcp_billing_export_v1_ABC",
                       date(2026, 11, 1), date(2026, 12, 1), client=SimpleNamespace(query=query))
    assert len(calls) == 2 and not calls[1]["job_config"].dry_run
    assert report["status"] == "no_export_rows_in_period" and report["totals_by_currency"] is None


@pytest.mark.parametrize("table", ["some-table", "project.dataset.table", "project.dataset.gcp_billing_export_v1_x`;DROP TABLE foo"])
def test_cost_table_reference_is_restricted(table):
    with pytest.raises(ValueError):
        query_for(table)


def test_cost_query_is_project_and_period_scoped_and_accounts_for_credits():
    query = query_for("pbmc-okf-portfolio.pbmc_billing_export.gcp_billing_export_v1_ABC")
    assert "project.id = @project" in query and "usage_start_time < TIMESTAMP(@end_date)" in query
    assert "UNNEST(credits)" in query and "currency" in query


def test_cost_dry_run_cap_rejects_query_before_charging_execution():
    calls = []

    def query(sql, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(total_bytes_processed=1001)

    with pytest.raises(ValueError, match="byte cap"):
        run_report("pbmc-okf-portfolio.pbmc_billing_export.gcp_billing_export_v1_ABC",
                   date(2026, 11, 1), date(2026, 12, 1), 1000, SimpleNamespace(query=query))
    assert len(calls) == 1 and calls[0]["job_config"].dry_run
