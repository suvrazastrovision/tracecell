import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from storage_fakes import FakeClient
from test_bigquery import FakeBigQuery
from pbmc_platform import run_cloud_dbt as cloud
from pbmc_platform.load_warehouse import load_bigquery
from pbmc_platform.publish_gcs import prepare_publish, publish_release


@pytest.fixture
def dbt_run(warehouse_release, tmp_path):
    plan, processed = warehouse_release
    storage, bq = FakeClient(), FakeBigQuery()
    publish_release(prepare_publish(processed, plan["release_id"], "pbmc-test-data", "pbmc-okf-portfolio"), storage)
    load_bigquery(plan, "pbmc-okf-portfolio", client=bq)
    bq.get_table = lambda name: SimpleNamespace(
        **{key: value for key, value in bq.tables[name].items() if key != "rows"},
        num_rows=len(bq.tables[name]["rows"]))
    bq.loads.clear()

    def runner(command, env, check, timeout):
        assert env["PBMC_RELEASE_ID"] == plan["release_id"]
        assert env["GOOGLE_CLOUD_PROJECT"] == "pbmc-okf-portfolio"
        assert command[1] == "build" and "--no-partial-parse" in command
        assert command[command.index("--target") + 1] == "bigquery"
        assert not check and timeout == 1100
        target = Path(command[command.index("--target-path") + 1])
        logs = Path(command[command.index("--log-path") + 1])
        target.mkdir(parents=True)
        logs.mkdir(parents=True)
        nodes = {"model.pbmc.fixture": {"resource_type": "model", "schema": f"pbmc_analytics_r_{plan['release_id']}"},
                 "test.pbmc.fixture": {"resource_type": "test"}}
        (target / "manifest.json").write_text(json.dumps({"metadata": {"invocation_id": "fixture"}, "nodes": nodes}))
        (target / "run_results.json").write_text(json.dumps({"metadata": {"invocation_id": "fixture"}, "results": [
            {"unique_id": key, "status": "success" if node["resource_type"] == "model" else "pass"}
            for key, node in nodes.items()]}))
        (logs / "dbt.log").write_text("fixture dbt log")
        return SimpleNamespace(returncode=0)

    return dict(bucket="pbmc-test-data", project="pbmc-okf-portfolio", release_id=plan["release_id"],
                storage_client=storage, bigquery_client=bq, work_dir=tmp_path / "dbt", runner=runner)


def saved(kwargs):
    return [json.loads(data) for name, (data, _) in kwargs["storage_client"].remote.objects.items()
            if name.startswith("runs/cloud_dbt/")]


def test_verified_sources_build_and_publish_all_artifacts(dbt_run):
    result = cloud.run_cloud_dbt(**dbt_run)
    assert result["models"] == result["tests"] == 1
    assert result["release_id"] == dbt_run["release_id"]
    assert dbt_run["bigquery_client"].loads == []
    assert saved(dbt_run)[0]["status"] == "success"
    assert len([name for name in dbt_run["storage_client"].remote.objects if name.startswith("runs/dbt/")]) == 3


@pytest.mark.parametrize("problem", ["rows", "description", "location", "manifest"])
def test_input_failure_blocks_dbt(dbt_run, problem):
    bq = dbt_run["bigquery_client"]
    if problem == "rows":
        next(iter(bq.tables.values()))["rows"].loc[0, "total_counts"] += 1
    elif problem == "description":
        next(iter(bq.tables.values()))["description"] = "wrong release"
    elif problem == "location":
        bq.location = "US"
    else:
        del dbt_run["storage_client"].remote.objects[f"processed/releases/{dbt_run['release_id']}/release.json"]
    dbt_run["runner"] = lambda *a, **k: pytest.fail("Unverified inputs reached dbt")
    with pytest.raises(ValueError):
        cloud.run_cloud_dbt(**dbt_run)
    assert saved(dbt_run)[0]["status"] == "failed"


@pytest.mark.parametrize("problem", ["exit", "test_fail", "skip", "missing", "invocation", "dataset"])
def test_dbt_failures_are_durable_and_never_report_success(dbt_run, problem):
    original = dbt_run["runner"]
    def runner(command, **kwargs):
        result = original(command, **kwargs)
        target = Path(command[command.index("--target-path") + 1])
        path = target / "run_results.json"
        data = json.loads(path.read_text())
        if problem == "exit":
            return SimpleNamespace(returncode=1)
        if problem in ("test_fail", "skip"):
            data["results"][1]["status"] = "fail" if problem == "test_fail" else "skipped"
        elif problem == "missing":
            data["results"].pop()
        elif problem == "invocation":
            data["metadata"]["invocation_id"] = "stale"
        else:
            manifest = json.loads((target / "manifest.json").read_text())
            manifest["nodes"]["model.pbmc.fixture"]["schema"] = "other_release"
            (target / "manifest.json").write_text(json.dumps(manifest))
        path.write_text(json.dumps(data))
        return result
    dbt_run["runner"] = runner
    with pytest.raises((RuntimeError, ValueError)):
        cloud.run_cloud_dbt(**dbt_run)
    assert saved(dbt_run)[0]["status"] == "failed"
    assert any(name.endswith("run_results.json") for name in dbt_run["storage_client"].remote.objects)


def test_audit_delivery_failure_fails_the_job(dbt_run):
    dbt_run["storage_client"].remote.fail_prefix = "runs/cloud_dbt/"
    with pytest.raises(RuntimeError, match="durable artifact upload failed"):
        cloud.run_cloud_dbt(**dbt_run)


def test_retry_cannot_reuse_stale_success(dbt_run):
    first = cloud.run_cloud_dbt(**dbt_run)
    dbt_run["runner"] = lambda *a, **k: SimpleNamespace(returncode=0)
    with pytest.raises(FileNotFoundError):
        cloud.run_cloud_dbt(**dbt_run)
    assert [item["status"] for item in saved(dbt_run)] == ["success", "failed"]
    assert saved(dbt_run)[1]["run_id"] != first["run_id"]
