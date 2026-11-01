"""Run isolated live failure/recovery drills and retain independently checked evidence.

Run once at a time from the repository root, with ADC and warehouse requirements.
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.cloud import bigquery, storage

PROJECT, REGION = "pbmc-okf-portfolio", "europe-west1"
RELEASE = "71a4bb4d026366b644c4aaf8bd4b29d8f8f87fe1701ba2e8d6a88ba164b83f7d"
BUCKET = f"{PROJECT}-drills"
RAW, ANALYTICS = "pbmc_drill_raw", f"pbmc_drill_analytics_r_{RELEASE}"
CASES = {
    "qc_fail": ("FAILED", ("qc",), "qc"),
    "missing_handoff": ("FAILED", ("qc",), None),
    "invalid_handoff": ("FAILED", ("qc",), None),
    "load_fail": ("FAILED", ("qc", "warehouse"), "warehouse"),
    "load_retry": ("SUCCEEDED", ("qc", "warehouse", "dbt"), None),
    "dbt_fail": ("FAILED", ("qc", "warehouse", "dbt"), "dbt"),
    "healthy": ("SUCCEEDED", ("qc", "warehouse", "dbt"), None),
}


def read_logs(api, query):
    """Continue searches even when Logging returns an empty continuation page."""
    body = {"resourceNames": [f"projects/{PROJECT}"], "filter": query,
            "pageSize": 1000, "orderBy": "timestamp asc"}
    entries = []
    while True:
        page = api("POST", "https://logging.googleapis.com/v2/entries:list", json=body)
        entries.extend(page.get("entries", []))
        if not page.get("nextPageToken"):
            return entries
        body = {**body, "pageToken": page["nextPageToken"]}


def main(resume=None):
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(credentials)
    bq, gcs = bigquery.Client(project=PROJECT), storage.Client(project=PROJECT)
    bucket, production = gcs.bucket(BUCKET), gcs.bucket(f"{PROJECT}-data")
    if resume:
        path = Path(resume).resolve()
        if path.name != "report.json" or path.parent.parent != Path("artifacts/drills").resolve():
            raise ValueError("Resume only an existing artifacts/drills/<timestamp>/report.json")
        root, report = path.parent, json.loads(path.read_text())
        if report["status"] != "failed":
            raise ValueError("Only a terminated, failed verification report can be resumed")
        report.update(status="running", resumed_at=datetime.now(timezone.utc).isoformat())
        report.pop("error", None)
        report.pop("finished_at", None)
    else:
        root = Path("artifacts/drills") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        root.mkdir(parents=True)
        report = {"started_at": datetime.now(timezone.utc).isoformat(), "status": "running", "cases": {}}

    def save():
        (root / "report.json").write_text(json.dumps(report, indent=2, default=str) + "\n")

    def api(method, url, **kwargs):
        response = session.request(method, url, timeout=120, **kwargs)
        response.raise_for_status()
        return response.json()

    def executions(stage):
        url = f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/pbmc-drill-{stage}/executions"
        found, params = {}, {"pageSize": 100}
        while True:
            page = api("GET", url, params=params)
            found.update({item["name"]: item for item in page.get("executions", [])})
            if not page.get("nextPageToken"):
                return found
            params["pageToken"] = page["nextPageToken"]

    def snapshot():
        objects = {blob.name: str(blob.generation) for blob in production.list_blobs(prefix=f"processed/releases/{RELEASE}/")}
        objects.update({blob.name: str(blob.generation) for blob in production.list_blobs(prefix="raw/")})
        tables = {}
        for dataset in ("pbmc_raw", f"pbmc_analytics_r_{RELEASE}"):
            for item in bq.list_tables(f"{PROJECT}.{dataset}"):
                table = bq.get_table(item.reference)
                tables[f"{dataset}.{table.table_id}"] = {"modified": table.modified.isoformat(), "rows": table.num_rows}
        return {"objects": objects, "tables": tables}

    def read_json(name):
        blob = bucket.get_blob(name)
        assert blob is not None, f"Missing durable evidence: {name}"
        data = blob.download_as_bytes(if_generation_match=int(blob.generation), checksum="crc32c", timeout=120)
        return json.loads(data)

    try:
        for stage in ("qc", "warehouse", "dbt"):
            assert all(item.get("completionTime") for item in executions(stage).values()), "An earlier drill is still running"
        current_snapshot = snapshot()
        if resume:
            assert report["production_before"] == current_snapshot, "Production changed since the earlier suite; start a fresh suite"
        else:
            report["production_before"] = current_snapshot
        save()
        # Copy only public, already verified source/release objects. Never overwrite.
        objects = list(production.list_blobs(prefix=f"processed/releases/{RELEASE}/"))
        import yaml
        manifest = yaml.safe_load(Path("configs/dataset.yaml").read_text())
        objects.append(production.get_blob(f"raw/{manifest['sha256']}/{manifest['source_filename']}"))
        for blob in objects:
            existing = bucket.get_blob(blob.name)
            if existing is None:
                production.copy_blob(blob, bucket, if_generation_match=0, source_generation=blob.generation)
                existing = bucket.get_blob(blob.name)
            assert existing.crc32c == blob.crc32c and existing.size == blob.size
        initial_raw = set(report["initial_raw_tables"]) if resume else {item.table_id for item in bq.list_tables(f"{PROJECT}.{RAW}") if bq.get_table(item.reference).num_rows}
        report["initial_raw_tables"] = sorted(initial_raw)
        execution_url = f"https://workflowexecutions.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/workflows/pbmc-pipeline-drills/executions"
        for scenario, (expected_state, stages, failed_stage) in CASES.items():
            prior = report["cases"].get(scenario)
            if prior and prior["status"] == "passed":
                print(f"Keeping verified case: {scenario}", flush=True)
                continue
            before = {stage: set(executions(stage)) for stage in ("qc", "warehouse", "dbt")}
            execution = api("POST", execution_url, json={"argument": json.dumps({"scenario": scenario})})
            case = {"workflow": execution["name"], "status": "running", "stages": {}}
            if prior:
                case["previous_attempts"] = prior.get("previous_attempts", []) + [{key: value for key, value in prior.items() if key != "previous_attempts"}]
            report["cases"][scenario] = case
            save()
            print(f"Started {scenario}: {execution['name']}", flush=True)
            deadline = time.monotonic() + 7800
            while execution["state"] in ("ACTIVE", "QUEUED"):
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Inspect still-running workflow manually: {execution['name']}")
                time.sleep(10)
                execution = api("GET", f"https://workflowexecutions.googleapis.com/v1/{execution['name']}")
            case["execution"] = execution
            assert execution["state"] == expected_state, execution
            if scenario == "missing_handoff":
                assert "receipt_metadata" in execution["error"]["context"]
            if scenario == "invalid_handoff":
                assert "unprovisioned_release" in execution["error"]["context"]
            for stage in ("qc", "warehouse", "dbt"):
                after = executions(stage)
                created = [after[key] for key in after.keys() - before[stage]]
                assert len(created) == (1 if stage in stages else 0), (scenario, stage, created)
                if not created:
                    continue
                item = created[0]
                assert item.get("failedCount", 0) == (1 if stage == failed_stage else 0), item
                assert item.get("succeededCount", 0) == (0 if stage == failed_stage else 1), item
                name = item["name"].split("/")[-1]
                assert item["template"]["serviceAccount"] == f"pbmc-drill-{stage}@{PROJECT}.iam.gserviceaccount.com"
                query = (f'resource.type="cloud_run_job" AND resource.labels.job_name="pbmc-drill-{stage}" '
                         f'AND labels."run.googleapis.com/execution_name"="{name}" AND timestamp>="{item["createTime"]}" AND '
                         '(jsonPayload.event="drill_attempt" OR jsonPayload.event="cloud_qc_run" OR '
                         'jsonPayload.event="cloud_warehouse_run" OR jsonPayload.event="cloud_dbt_run")')
                entries = []
                expected_audits = 0 if stage == failed_stage and scenario in ("qc_fail", "load_fail") else (2 if (scenario, stage) in (("load_retry", "warehouse"), ("dbt_fail", "dbt")) else 1)
                for retry in range(6):
                    entries = read_logs(api, query)
                    attempts = sorted({int(e["jsonPayload"]["attempt"]) for e in entries if e["jsonPayload"]["event"] == "drill_attempt"})
                    expected_attempts = [0, 1] if stage == failed_stage or (scenario == "load_retry" and stage == "warehouse") else [0]
                    audit_count = sum(e["jsonPayload"]["event"] != "drill_attempt" for e in entries)
                    if attempts == expected_attempts and audit_count == expected_audits:
                        break
                    time.sleep(5)
                assert attempts == expected_attempts, (scenario, stage, attempts)
                assert audit_count == expected_audits, (scenario, stage, audit_count)
                evidence = {"execution": item, "attempts": attempts, "audits": []}
                for entry in entries:
                    audit = entry["jsonPayload"]
                    if audit["event"] == "drill_attempt":
                        continue
                    prefix = {"qc": "cloud_qc", "warehouse": "cloud_warehouse", "dbt": "cloud_dbt"}[stage]
                    durable = read_json(f"runs/{prefix}/{audit['run_id']}.json")
                    assert durable == {key: value for key, value in audit.items() if key != "event"}
                    evidence["audits"].append(durable)
                    if stage == "dbt":
                        results = read_json(f"runs/dbt/{audit['run_id']}/run_results.json")
                        manifest = read_json(f"runs/dbt/{audit['run_id']}/manifest.json")
                        assert manifest["metadata"]["invocation_id"] == results["metadata"]["invocation_id"]
                        log = bucket.get_blob(f"runs/dbt/{audit['run_id']}/dbt.log")
                        assert log and log.size > 0
                        statuses = [(r["unique_id"], r["status"]) for r in results["results"]]
                        if scenario == "dbt_fail":
                            assert [(key, status) for key, status in statuses if status == "fail"]
                            assert all("drill_forced_failure" in key for key, status in statuses if status == "fail")
                            assert sum(status == "pass" for _, status in statuses) == 30
                        else:
                            assert len(statuses) == 36 and all(status in ("success", "pass") for _, status in statuses)
                            assert sum(key.startswith("model.") and status == "success" for key, status in statuses) == 6
                            assert sum(key.startswith("test.") and status == "pass" for key, status in statuses) == 30
                if scenario == "load_retry" and stage == "warehouse":
                    assert [audit["status"] for audit in evidence["audits"]] == ["failed", "success"]
                    assert evidence["audits"][-1]["result"]["tables"]["cells"]["reused"]
                if scenario == "healthy" and stage == "warehouse":
                    assert evidence["audits"][-1]["result"]["reused"]
                    assert all(t["job_id"] is None for t in evidence["audits"][-1]["result"]["tables"].values())
                case["stages"][stage] = evidence
            case["status"] = "passed"
            save()
            print(f"PASS {scenario}: {expected_state}; downstream starts and task attempts verified", flush=True)
        # Bounded independent readback after recovery.
        summary = dict(next(iter(bq.list_rows(f"{PROJECT}.{ANALYTICS}.mart_dataset_qc"))).items())
        assert (summary["cells_total"], summary["cells_pass"], summary["cells_fail"], summary["total_counts"]) == (2700, 2638, 62, 6390631)
        assert bq.get_table(f"{PROJECT}.{ANALYTICS}.mart_cohort_cells").num_rows == 2638
        report["recovered_summary"] = summary
        load_jobs = []
        for job in bq.list_jobs(all_users=True, min_creation_time=datetime.fromisoformat(report["started_at"]), max_results=1000):
            if job.job_type == "load" and job.destination and job.destination.dataset_id == RAW:
                assert not job.error_result
                load_jobs.append({"job_id": job.job_id, "table": job.destination.table_id})
        expected_tables = {f"{name}_r_{RELEASE}" for name in ("cells", "genes", "dataset_summary")}
        assert len(load_jobs) == len(expected_tables - initial_raw), load_jobs
        assert len({job["table"] for job in load_jobs}) == len(load_jobs), "Duplicate load jobs detected"
        report["new_load_jobs"] = load_jobs
        report["production_after"] = snapshot()
        assert report["production_before"] == report["production_after"], "Production snapshot changed; investigate concurrent jobs"
        report["status"] = "success"
        print(f"All seven drills passed; production unchanged. Evidence: {root / 'report.json'}", flush=True)
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        for case in report["cases"].values():
            if case["status"] == "running":
                case["status"] = "failed_verification"
        raise
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", help="Resume a failed local report, retaining its already verified cases")
    main(**vars(parser.parse_args()))
