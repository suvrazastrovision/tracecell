"""Build and test release-scoped analytics from verified BigQuery source tables."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .cloud_release import download_release
from .ingestion import canonical_json, sha256_file
from .load_warehouse import compare_rows, prepare_load, table_description, validate_identifier
from .publish_gcs import publish_object, validate_destination
from .run_qc import write_json


def verify_sources(client, plan, project, raw_dataset, analytics_dataset, location):
    """Read only: require existing colocated datasets and exact producer rows."""
    for dataset in (raw_dataset, analytics_dataset):
        if client.get_dataset(f"{project}.{dataset}").location.lower() != location.lower():
            raise ValueError(f"Dataset {dataset} is in a different location")
    for name, spec in plan["tables"].items():
        table_id = f"{project}.{raw_dataset}.{spec['table_name']}"
        table = client.get_table(table_id)
        schema = [(field.name, field.field_type, field.mode) for field in table.schema]
        expected = [(field["name"], field["type"], "NULLABLE") for field in spec["schema"]]
        if (table.description != table_description(plan, name) or schema != expected
                or table.num_rows != spec["rows"]):
            raise ValueError(f"Raw table source contract differs: {table_id}")
        actual = pd.DataFrame([dict(row.items()) for row in client.list_rows(table_id)])
        compare_rows(name, actual, pd.read_parquet(spec["file"]))


def summarize_results(target, invocation_id, release_id, dataset):
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    results = json.loads((target / "run_results.json").read_text(encoding="utf-8"))
    if results["metadata"]["invocation_id"] != manifest["metadata"]["invocation_id"]:
        raise ValueError("dbt artifacts belong to different invocations")
    nodes = manifest["nodes"]
    expected = {key for key, node in nodes.items() if node["resource_type"] in ("model", "test")}
    seen = set()
    counts = {"models": 0, "tests": 0}
    for item in results["results"]:
        key = item["unique_id"]
        if key in seen or key not in expected:
            raise ValueError("Unexpected or duplicate dbt result")
        seen.add(key)
        node = nodes[key]
        kind = node["resource_type"]
        if item["status"] != ("success" if kind == "model" else "pass"):
            raise ValueError(f"dbt {key} finished with status {item['status']}")
        if kind == "model" and node["schema"] != dataset:
            raise ValueError("dbt model targeted a different release dataset")
        counts["models" if kind == "model" else "tests"] += 1
    if seen != expected or not all(counts.values()):
        raise ValueError("dbt build did not complete every model and test")
    return {**counts, "dbt_invocation_id": results["metadata"]["invocation_id"],
            "run_id": invocation_id, "release_id": release_id, "analytics_dataset": dataset}


def run_cloud_dbt(bucket, project, release_id, raw_dataset="pbmc_raw", location="europe-west1",
                  analytics_base="pbmc_analytics", project_dir="warehouse/dbt", work_dir=None,
                  storage_client=None, bigquery_client=None, runner=None):
    if work_dir is None:
        with tempfile.TemporaryDirectory(prefix="pbmc-cloud-dbt-") as temp:
            return run_cloud_dbt(bucket, project, release_id, raw_dataset, location, analytics_base,
                                 project_dir, temp, storage_client, bigquery_client, runner)
    # A unique directory per invocation prevents reuse of stale successful dbt artifacts.
    run_id = uuid.uuid4().hex
    root = Path(work_dir).resolve() / run_id
    target, logs = root / "target", root / "logs"
    root.mkdir(parents=True)
    audit = {"run_id": run_id, "started_at": datetime.now(timezone.utc).isoformat(),
             "status": "running", "project": project, "bucket": bucket, "release_id": release_id,
             "raw_dataset": raw_dataset, "location": location,
             "cloud_execution": os.environ.get("CLOUD_RUN_EXECUTION")}
    remote = None
    try:
        validate_destination(bucket, project)
        for value in (raw_dataset, analytics_base):
            validate_identifier(value, "dataset")
        if not isinstance(release_id, str) or not re.fullmatch(r"[a-f0-9]{64}", release_id):
            raise ValueError("Expected a full SHA-256 release ID")
        if not re.fullmatch(r"[a-zA-Z0-9-]+", location):
            raise ValueError("Invalid BigQuery location")
        dataset = f"{analytics_base}_r_{release_id}"
        audit["analytics_dataset"] = dataset
        if storage_client is None:
            from google.cloud import storage
            storage_client = storage.Client(project=project)
        remote = storage_client.bucket(bucket)
        download_release(remote, release_id, root / "processed")
        plan = prepare_load(root / "processed", release_id)
        marker = root / "processed" / "releases" / release_id / "release.json"
        audit["input_manifest"] = {"uri": f"gs://{bucket}/processed/releases/{release_id}/release.json",
                                   "sha256": sha256_file(marker)}
        if bigquery_client is None:
            from google.cloud import bigquery
            bigquery_client = bigquery.Client(project=project, location=location)
        verify_sources(bigquery_client, plan, project, raw_dataset, dataset, location)
        dbt_project = Path(project_dir).resolve()
        if not (dbt_project / "dbt_project.yml").is_file():
            raise ValueError("dbt project not found")
        environment = {**os.environ, "GOOGLE_CLOUD_PROJECT": project, "PBMC_RELEASE_ID": release_id,
                       "PBMC_RAW_DATASET": raw_dataset, "PBMC_BQ_LOCATION": location,
                       "PBMC_ANALYTICS_DATASET": analytics_base, "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
        command = [shutil.which("dbt") or "dbt", "build", "--project-dir", str(dbt_project),
                   "--profiles-dir", str(dbt_project), "--target", "bigquery",
                   "--target-path", str(target), "--log-path", str(logs), "--no-partial-parse"]
        completed = (runner or subprocess.run)(command, env=environment, check=False, timeout=1100)
        audit["dbt_exit_code"] = completed.returncode
        if completed.returncode != 0:
            raise RuntimeError(f"dbt build failed with exit code {completed.returncode}")
        result = summarize_results(target, run_id, release_id, dataset)
        result.update(audit_uri=f"gs://{bucket}/runs/cloud_dbt/{run_id}.json",
                      artifacts_uri=f"gs://{bucket}/runs/dbt/{run_id}/",
                      cloud_execution=audit["cloud_execution"])
        audit.update(status="success", result=result)
        return result
    except Exception as error:
        audit.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        audit["finished_at"] = datetime.now(timezone.utc).isoformat()
        path = root / "audit.json"
        write_json(path, audit)
        print(canonical_json({"event": "cloud_dbt_run", **audit}), flush=True)
        failures = []
        if remote is not None:
            records = [(target / name, f"runs/dbt/{run_id}/{name}")
                       for name in ("manifest.json", "run_results.json")]
            records += [(logs / "dbt.log", f"runs/dbt/{run_id}/dbt.log"),
                        (path, f"runs/cloud_dbt/{run_id}.json")]
            for local, name in records:
                if not local.is_file():
                    continue
                try:
                    publish_object(remote, {"file": str(local), "object_name": name,
                                            "sha256": sha256_file(local)})
                except Exception as error:
                    failures.append(f"{type(error).__name__}: {error}")
                    print(canonical_json({"event": "cloud_dbt_audit_upload_failed", "run_id": run_id,
                                          "object_name": name, "error": failures[-1]}),
                          file=sys.stderr, flush=True)
        if failures and audit["status"] == "success":
            raise RuntimeError("dbt completed, but durable artifact upload failed: " + "; ".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bucket", "project", "release-id"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--raw-dataset", default="pbmc_raw")
    parser.add_argument("--location", default="europe-west1")
    parser.add_argument("--analytics-base", default="pbmc_analytics")
    parser.add_argument("--project-dir", default="warehouse/dbt")
    parser.add_argument("--work-dir")
    try:
        print(json.dumps(run_cloud_dbt(**vars(parser.parse_args())), indent=2))
    except Exception as error:
        parser.exit(1, f"Cloud dbt build failed: {error}\n")


if __name__ == "__main__":
    main()
