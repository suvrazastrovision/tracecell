"""Fault injection packaged only in the isolated knowledge drill image."""

import argparse
import json
import os
from pathlib import Path

SCENARIOS = ("invalid_handoff", "validation_fail", "publication_retry", "completion_retry", "healthy")


def validate_isolation(project, bucket, raw_dataset, analytics_base, scenario):
    if (project != "pbmc-okf-portfolio" or bucket != "pbmc-okf-portfolio-drills"
            or raw_dataset != "pbmc_drill_raw" or analytics_base != "pbmc_drill_analytics"
            or scenario not in SCENARIOS):
        raise ValueError("Knowledge fault injection requires explicitly isolated drill resources")


def run(project, bucket, release_id, dbt_run_id, dbt_execution, dbt_handoff_generation,
        raw_dataset="pbmc_drill_raw", analytics_base="pbmc_drill_analytics", location="europe-west1"):
    scenario = os.environ.get("PBMC_KNOWLEDGE_DRILL_SCENARIO")
    validate_isolation(project, bucket, raw_dataset, analytics_base, scenario)
    attempt = int(os.environ["CLOUD_RUN_TASK_ATTEMPT"])
    print(json.dumps({"event": "knowledge_drill_attempt", "scenario": scenario, "attempt": attempt,
                      "execution": os.environ["CLOUD_RUN_EXECUTION"]}), flush=True)
    from pbmc_platform import run_cloud_knowledge as worker
    if scenario == "invalid_handoff":
        dbt_run_id = "0" * 32
    elif scenario == "validation_fail":
        original = worker.run_validate

        def invalid(package_path, *args, **kwargs):
            path = Path(package_path)
            package = json.loads(path.read_text())
            package["results"]["metrics"]["cells_total"] += 1
            path.write_text(json.dumps(package), encoding="utf-8")
            return original(package_path, *args, **kwargs)

        worker.run_validate = invalid
    elif scenario == "publication_retry" and attempt == 0:
        from pbmc_platform import publish_knowledge as publisher
        original = publisher.publish_object

        def interrupted(remote, spec):
            if spec["object_name"].startswith("knowledge/releases/") and spec["object_name"].endswith("/report.md"):
                raise RuntimeError("DRILL: publication interrupted after knowledge.json committed")
            return original(remote, spec)

        publisher.publish_object = interrupted
    result = worker.run_cloud_knowledge(project, bucket, release_id, dbt_run_id, dbt_execution,
                                         dbt_handoff_generation, raw_dataset, analytics_base, location)
    if scenario == "completion_retry" and attempt == 0:
        raise RuntimeError("DRILL: task interrupted after durable completion receipt")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("project", "bucket", "release-id", "dbt-run-id", "dbt-execution", "dbt-handoff-generation"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--raw-dataset", default="pbmc_drill_raw")
    parser.add_argument("--analytics-base", default="pbmc_drill_analytics")
    parser.add_argument("--location", default="europe-west1")
    print(json.dumps(run(**vars(parser.parse_args())), indent=2))
