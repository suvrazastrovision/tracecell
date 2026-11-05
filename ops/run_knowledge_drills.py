"""Run five isolated live knowledge failure/recovery scenarios and preserve checked evidence.

Use once at a time from the repository root. No production worker or table is modified.
"""

import argparse
import json
from pathlib import Path
import tempfile
import time
from datetime import datetime, timezone

import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.cloud import bigquery, storage

from ops.run_drills import read_logs
from pbmc_platform.knowledge_handoff import read_json_object, validate_dbt_receipt
from pbmc_platform.run_cloud_knowledge import restore_completed

PROJECT, REGION = "pbmc-okf-portfolio", "europe-west1"
RELEASE = "71a4bb4d026366b644c4aaf8bd4b29d8f8f87fe1701ba2e8d6a88ba164b83f7d"
BUCKET = f"{PROJECT}-drills"
CASES = {"invalid_handoff": "FAILED", "validation_fail": "FAILED", "publication_retry": "SUCCEEDED",
         "completion_retry": "SUCCEEDED", "healthy": "SUCCEEDED"}


def verify_expected_fault(scenario, audits, validation_reports, logs):
    """Reject unrelated infrastructure errors as evidence of the requested fault."""
    if scenario == "invalid_handoff":
        reason = "Selected dbt run differs from execution receipt"
        assert len(audits) == 2 and all(reason in a.get("error", "") for a in audits), "Wrong handoff failure reason"
    elif scenario == "validation_fail":
        reason = "Package metrics differs: cells_total"
        assert len(validation_reports) == 2 and all(reason in a.get("error", "") for a in validation_reports), "Wrong validation failure reason"
    elif scenario == "publication_retry":
        reason = "DRILL: publication interrupted after knowledge.json committed"
        assert len(audits) == 2 and reason in audits[0].get("error", ""), "Wrong publication failure reason"
    elif scenario == "completion_retry":
        reason = "DRILL: task interrupted after durable completion receipt"
        assert any(reason in json.dumps(entry) for entry in logs), "Completion fault was not observed in logs"
    else:
        reason = None
    return {"scenario": scenario, "expected_fault_reason": reason, "verified": True}


def main(resume=None):
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(credentials)
    gcs, bq = storage.Client(project=PROJECT), bigquery.Client(project=PROJECT)
    bucket, production = gcs.bucket(BUCKET), gcs.bucket(f"{PROJECT}-data")
    if resume:
        path = Path(resume).resolve()
        if path.name != "report.json" or path.parent.parent != Path("artifacts/knowledge_drills").resolve():
            raise ValueError("Resume an artifacts/knowledge_drills/<run>/report.json file only")
        report, root = json.loads(path.read_text()), path.parent
        if report["status"] != "failed":
            raise ValueError("Resume only a failed verification run")
        report.update(status="running", resumed_at=datetime.now(timezone.utc).isoformat())
        report.pop("error", None)
        report.pop("finished_at", None)
    else:
        root = Path("artifacts/knowledge_drills") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        root.mkdir(parents=True)
        report = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(), "cases": {}}

    def save():
        (root / "report.json").write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")

    def api(method, url, **kwargs):
        response = session.request(method, url, timeout=120, **kwargs)
        response.raise_for_status()
        return response.json()

    def objects(prefix):
        return {blob.name: int(blob.generation) for blob in bucket.list_blobs(prefix=prefix)}

    def tables(raw, analytics):
        output = {}
        for dataset in (raw, analytics):
            for item in bq.list_tables(f"{PROJECT}.{dataset}"):
                table = bq.get_table(item.reference)
                output[f"{dataset}.{table.table_id}"] = {"modified": str(table.modified), "rows": table.num_rows, "etag": table.etag}
        return output

    def production_snapshot():
        return {"objects": {blob.name: str(blob.generation) for blob in production.list_blobs()},
                "tables": tables("pbmc_raw", f"pbmc_analytics_r_{RELEASE}")}

    def executions(job):
        result, params = {}, {"pageSize": 100}
        while True:
            page = api("GET", f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/{job}/executions", params=params)
            result.update({row["name"]: row for row in page.get("executions", [])})
            if not page.get("nextPageToken"):
                return result
            params["pageToken"] = page["nextPageToken"]

    def read(name):
        blob = bucket.get_blob(name, timeout=120)
        assert blob is not None, f"Missing evidence: {name}"
        payload = blob.download_as_bytes(if_generation_match=int(blob.generation), checksum="crc32c", timeout=120)
        return json.loads(payload)

    try:
        current = production_snapshot()
        if resume:
            assert current == report["production_before"], "Production changed since this suite started"
        else:
            report["production_before"] = current
            for job in ("pbmc-drill-dbt-handoff", "pbmc-drill-knowledge"):
                assert all(row.get("completionTime") for row in executions(job).values()), "Another drill is still running"
        save()
        # Reuse the public committed release; copy missing objects create-only.
        for source in production.list_blobs(prefix=f"processed/releases/{RELEASE}/"):
            existing = bucket.get_blob(source.name, timeout=120)
            if existing is None:
                production.copy_blob(source, bucket, if_generation_match=0, source_generation=source.generation)
                existing = bucket.get_blob(source.name, timeout=120)
            assert existing.crc32c == source.crc32c and existing.size == source.size
        if not report.get("dbt", {}).get("execution"):
            if not report.get("dbt", {}).get("operation"):
                operation = api("POST", f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/pbmc-drill-dbt-handoff:run", json={})
                report["dbt"] = {"operation": operation["name"]}
                save()
            operation_deadline = time.monotonic() + 3000
            while True:
                operation = api("GET", f"https://run.googleapis.com/v2/{report['dbt']['operation']}")
                execution_name = operation.get("response", {}).get("name") or operation.get("metadata", {}).get("name")
                if execution_name and "/executions/" in execution_name:
                    report["dbt"]["execution"] = execution_name
                    save()
                    break
                if operation.get("error"):
                    raise RuntimeError(operation["error"])
                assert time.monotonic() < operation_deadline, "dbt run operation did not expose an execution"
                time.sleep(10)
        deadline = time.monotonic() + 3000
        while True:
            dbt = api("GET", f"https://run.googleapis.com/v2/{report['dbt']['execution']}")
            if dbt.get("completionTime"):
                break
            assert time.monotonic() < deadline, "dbt preparation timed out"
            time.sleep(10)
        assert dbt.get("succeededCount") == 1 and not dbt.get("failedCount", 0), dbt
        assert dbt["template"]["serviceAccount"] == f"pbmc-drill-dbt@{PROJECT}.iam.gserviceaccount.com"
        dbt_execution = dbt["name"].split("/")[-1]
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            receipt, generation = read_json_object(bucket, f"runs/handoffs/dbt/{dbt_execution}.json", temp / "receipt.json")
            summary = validate_dbt_receipt(bucket, temp, receipt, PROJECT, BUCKET, RELEASE, dbt_execution,
                                           "pbmc_drill_raw", f"pbmc_drill_analytics_r_{RELEASE}", REGION)
            assert summary["models"] == 6 and summary["tests"] == 30
        report["dbt"].update(record=dbt, receipt=receipt, receipt_generation=generation)
        if "isolated_tables_before" not in report:
            report["isolated_tables_before"] = tables("pbmc_drill_raw", f"pbmc_drill_analytics_r_{RELEASE}")
        if "isolated_sources_before" not in report:
            report["isolated_sources_before"] = objects(f"processed/releases/{RELEASE}/")
        save()
        for scenario, expected in CASES.items():
            case = report["cases"].get(scenario, {})
            if case.get("status") == "passed":
                continue
            if not case.get("workflow"):
                case = {"status": "running", "knowledge_before": objects("knowledge/releases/"),
                        "executions_before": sorted(executions("pbmc-drill-knowledge")),
                        "publication_audits_before": objects("runs/knowledge_publication/")}
                report["cases"][scenario] = case
                body = {"argument": json.dumps({"scenario": scenario, "dbt_execution": dbt_execution})}
                workflow = api("POST", f"https://workflowexecutions.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/workflows/pbmc-knowledge-drills/executions", json=body)
                case["workflow"] = workflow["name"]
                save()
            print(f"Checking knowledge scenario: {scenario}", flush=True)
            deadline = time.monotonic() + 4500
            while True:
                workflow = api("GET", f"https://workflowexecutions.googleapis.com/v1/{case['workflow']}")
                if workflow["state"] != "ACTIVE":
                    break
                assert time.monotonic() < deadline, "Workflow wait expired; inspect before rerunning"
                time.sleep(10)
            case["workflow_record"] = workflow
            save()
            assert workflow["state"] == expected, workflow
            started = set(executions("pbmc-drill-knowledge")) - set(case["executions_before"])
            assert len(started) == 1, started
            execution = api("GET", f"https://run.googleapis.com/v2/{started.pop()}")
            execution_name = execution["name"].split("/")[-1]
            assert execution.get("completionTime")
            assert execution["creator"].removeprefix("serviceAccount:") == f"pbmc-drill-workflow@{PROJECT}.iam.gserviceaccount.com"
            assert execution["template"]["serviceAccount"] == f"pbmc-drill-knowledge@{PROJECT}.iam.gserviceaccount.com"
            assert execution.get("succeededCount", 0) == (1 if expected == "SUCCEEDED" else 0)
            assert execution.get("failedCount", 0) == (1 if expected == "FAILED" else 0)
            query = (f'resource.type="cloud_run_job" AND resource.labels.job_name="pbmc-drill-knowledge" '
                     f'AND labels."run.googleapis.com/execution_name"="{execution_name}" '
                     f'AND timestamp>="{execution["createTime"]}"')
            expected_attempts = [0] if scenario == "healthy" else [0, 1]
            for _ in range(8):
                logs = read_logs(api, query)
                attempts = sorted(int(row["jsonPayload"]["attempt"]) for row in logs
                                  if row.get("jsonPayload", {}).get("event") == "knowledge_drill_attempt")
                cloud_audits = [row["jsonPayload"] for row in logs if row.get("jsonPayload", {}).get("event") == "cloud_knowledge_run"]
                completion_fault_visible = scenario != "completion_retry" or any(
                    "DRILL: task interrupted after durable completion receipt" in json.dumps(entry) for entry in logs)
                if attempts == expected_attempts and len(cloud_audits) == len(expected_attempts) and completion_fault_visible:
                    break
                time.sleep(10)
            assert attempts == expected_attempts and len(cloud_audits) == len(expected_attempts)
            durable, validation_reports = [], []
            for logged in cloud_audits:
                audit = read(f"runs/cloud_knowledge/{logged['run_id']}.json")
                assert audit == {k: v for k, v in logged.items() if k != "event"}
                durable.append(audit)
                validation_reports += [read(name) for name in objects(f"runs/knowledge_validation/{logged['run_id']}/")]
            statuses = {"invalid_handoff": ["failed", "failed"], "validation_fail": ["failed", "failed"],
                        "publication_retry": ["failed", "success"], "completion_retry": ["success", "success"], "healthy": ["success"]}
            assert [a["status"] for a in durable] == statuses[scenario]
            expected_reports = {"invalid_handoff": 0, "validation_fail": 2, "publication_retry": 2, "completion_retry": 1, "healthy": 1}
            assert len(validation_reports) == expected_reports[scenario]
            assert all(a["status"] == ("failed" if scenario == "validation_fail" else "verified") for a in validation_reports)
            fault_evidence = verify_expected_fault(scenario, durable, validation_reports, logs)
            now = objects("knowledge/releases/")
            assert all(now.get(name) == old for name, old in case["knowledge_before"].items())
            added = set(now) - set(case["knowledge_before"])
            markers = [name for name in added if name.endswith("/publication.json")]
            assert len(markers) == (0 if expected == "FAILED" else 1)
            completed_receipt = bucket.get_blob(f"runs/handoffs/knowledge/{execution_name}.json", timeout=120)
            if expected == "FAILED":
                assert completed_receipt is None and not added
            else:
                outcome = json.loads(workflow["result"])
                assert outcome["knowledge_execution"] == execution_name and outcome["dbt_run_id"] == summary["run_id"]
                with tempfile.TemporaryDirectory() as temp:
                    temp = Path(temp)
                    completed, _ = read_json_object(bucket, f"runs/handoffs/knowledge/{execution_name}.json", temp / "completion.json",
                                                    outcome["knowledge_handoff_generation"])
                    publication = restore_completed(bucket, temp, completed, PROJECT, BUCKET, RELEASE, execution_name,
                                                      dbt_execution, summary["run_id"], generation)
                    assert publication["package_id"] == outcome["knowledge_package_id"]
                    validation = json.loads((temp / "completed-bundle/validation.json").read_text())
                    assert validation["cohort_rows"] == 2638 and validation["metrics"]["cells_total"] == 2700
                    case["verified_validation"] = validation
                    case["publication"] = publication
            if scenario == "completion_retry":
                assert [a["reused"] for a in durable] == [False, True]
                assert durable[0]["result"]["package_id"] == durable[1]["result"]["package_id"]
                assert len(added) == 4
            if scenario == "publication_retry":
                assert len(added) == 5
                new_audits = set(objects("runs/knowledge_publication/")) - set(case["publication_audits_before"])
                publisher_audits = [read(name) for name in new_audits]
                assert sorted(a["status"] for a in publisher_audits) == ["failed", "success"]
                failed = next(a for a in publisher_audits if a["status"] == "failed")
                partial = f"knowledge/releases/{RELEASE}/{failed['package_id']}/knowledge.json"
                assert now[partial] == failed["objects"]["knowledge.json"]["generation"]
                assert f"knowledge/releases/{RELEASE}/{failed['package_id']}/publication.json" not in now
                case["publication_attempts"] = publisher_audits
            case.update(status="passed", execution=execution, attempts=attempts, cloud_audits=durable,
                        validation_reports=validation_reports, newly_created_objects=sorted(added), fault_evidence=fault_evidence)
            save()
            print(f"Passed: {scenario}", flush=True)
        report["production_after"] = production_snapshot()
        assert report["production_after"] == report["production_before"], "Production data changed"
        assert tables("pbmc_drill_raw", f"pbmc_drill_analytics_r_{RELEASE}") == report["isolated_tables_before"]
        assert objects(f"processed/releases/{RELEASE}/") == report["isolated_sources_before"]
        report.update(status="success", finished_at=datetime.now(timezone.utc).isoformat())
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}", finished_at=datetime.now(timezone.utc).isoformat())
        save()
        raise
    save()
    print(json.dumps({"status": "success", "cases_passed": len(report["cases"]), "report": str((root / 'report.json').resolve())}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume")
    main(**vars(parser.parse_args()))
