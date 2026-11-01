"""Fault injection entrypoint packaged ONLY in isolated drill images."""
import argparse
import copy
import json
import os
import shutil
import tempfile
from pathlib import Path

SCENARIOS = ("qc_fail", "missing_handoff", "invalid_handoff", "load_fail", "load_retry", "dbt_fail", "healthy")


def validate_isolation(project, bucket, raw_dataset, analytics_base, stage, scenario):
    if (project != "pbmc-okf-portfolio" or bucket != "pbmc-okf-portfolio-drills"
            or raw_dataset != "pbmc_drill_raw" or analytics_base != "pbmc_drill_analytics"
            or stage not in ("qc", "warehouse", "dbt") or scenario not in SCENARIOS):
        raise ValueError("Fault injection requires the explicitly isolated drill resources")


def fail_second_table(client):
    original = client.create_table
    calls = 0
    def load(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("DRILL: transient failure after first table committed or verified")
        return original(*args, **kwargs)
    client.create_table = load
    return client


def prepare_dbt_project(destination, inject_failure=False, source_project="warehouse/dbt"):
    """Modify only a private copy; container source directories may be read-only."""
    destination = Path(destination)
    shutil.copytree(source_project, destination, ignore=shutil.ignore_patterns("target*", "logs", "dbt_packages"))
    destination.chmod(0o700)
    if inject_failure:
        tests = destination / "tests"
        tests.mkdir(exist_ok=True)
        tests.chmod(0o700)
        (tests / "drill_forced_failure.sql").write_text("select 1 as injected_failure\n")
    return destination


def run(project, bucket, release_id=None, raw_dataset="pbmc_drill_raw", location="europe-west1",
        analytics_base="pbmc_drill_analytics"):
    stage = os.environ.get("PBMC_DRILL_STAGE")
    scenario = os.environ.get("PBMC_DRILL_SCENARIO")
    validate_isolation(project, bucket, raw_dataset, analytics_base, stage, scenario)
    attempt = int(os.environ["CLOUD_RUN_TASK_ATTEMPT"])
    print(json.dumps({"event": "drill_attempt", "stage": stage, "scenario": scenario,
                      "attempt": attempt, "execution": os.environ["CLOUD_RUN_EXECUTION"]}), flush=True)
    if (stage, scenario) in (("qc", "qc_fail"), ("warehouse", "load_fail")):
        raise RuntimeError(f"DRILL: permanent {stage} failure")
    if stage == "qc":
        from pbmc_platform import run_cloud_qc as qc
        if scenario == "missing_handoff":
            qc.publish_handoff = lambda *args: None
        elif scenario == "invalid_handoff":
            original = qc.publish_handoff
            def invalid(remote, root, result, execution):
                wrong = copy.deepcopy(result)
                wrong["release_id"] = "0" * 64
                return original(remote, root, wrong, execution)
            qc.publish_handoff = invalid
        return qc.run_cloud_qc(bucket=bucket, project=project)
    if stage == "warehouse":
        from google.cloud import bigquery
        from pbmc_platform.run_cloud_warehouse import run_cloud_load
        client = bigquery.Client(project=project, location=location)
        if scenario == "load_retry" and attempt == 0:
            fail_second_table(client)
        return run_cloud_load(bucket, project, release_id, raw_dataset, location, bigquery_client=client)
    from pbmc_platform.run_cloud_dbt import run_cloud_dbt
    with tempfile.TemporaryDirectory(prefix="drill-dbt-") as temp:
        source = prepare_dbt_project(Path(temp) / "dbt", scenario == "dbt_fail")
        return run_cloud_dbt(bucket, project, release_id, raw_dataset, location, analytics_base, project_dir=source)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--release-id")
    parser.add_argument("--raw-dataset", default="pbmc_drill_raw")
    parser.add_argument("--location", default="europe-west1")
    parser.add_argument("--analytics-base", default="pbmc_drill_analytics")
    print(json.dumps(run(**vars(parser.parse_args())), indent=2))
