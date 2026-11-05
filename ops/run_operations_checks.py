"""Live lock/recovery, private alert delivery and scheduled production acceptance."""

import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile
import time

import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage

from pbmc_platform.knowledge_handoff import read_json_object
from pbmc_platform.recover_pipeline_lock import release_retained_lock
from pbmc_platform.run_cloud_knowledge import restore_completed


PROJECT, REGION = "pbmc-okf-portfolio", "europe-west1"
DRILL = "pbmc-pipeline-guard-drill"
PRODUCTION = "pbmc-qc-workflow"


def verify_production_result(result, gcs, api):
    outcome = json.loads(result["result"])
    bucket = gcs.bucket(f"{PROJECT}-data")
    assert outcome["status"] == "success" and bucket.get_blob("operations/locks/production-pipeline.json", timeout=120) is None
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        receipt, _ = read_json_object(bucket, f"runs/handoffs/knowledge/{outcome['knowledge_execution']}.json", temp / "receipt.json", outcome["knowledge_handoff_generation"])
        publication = restore_completed(bucket, temp, receipt, PROJECT, bucket.name, outcome["release_id"], outcome["knowledge_execution"],
                                        outcome["dbt_execution"], outcome["dbt_run_id"], outcome["dbt_handoff_generation"])
        validation = json.loads((temp / "completed-bundle/validation.json").read_text())
        assert validation["cohort_rows"] == 2638
    scheduler = api("GET", f"https://cloudscheduler.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/jobs/pbmc-weekly-pipeline")
    assert scheduler.get("status", {}).get("code", 0) == 0
    return dict(production_result=result, publication=publication, production_validation=validation, scheduler=scheduler)


def resume_production(path, execution):
    path = Path(path).resolve()
    if path.name != "report.json" or path.parent.parent != Path("artifacts/operations").resolve():
        raise ValueError("Select an existing artifacts/operations/<run>/report.json")
    if not execution or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", execution):
        raise ValueError("Select the exact existing production workflow UUID")
    report = json.loads(path.read_text())
    assert report["status"] == "failed" and all(case["status"] == "passed" for case in report["cases"].values())
    assert len(report["cases"]) == 6 and report["alert_delivery"]["status"] == "passed"
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(credentials)

    def api(method, url, **kwargs):
        response = session.request(method, url, timeout=120, **kwargs)
        response.raise_for_status()
        return response.json()

    url = f"https://workflowexecutions.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/workflows/{PRODUCTION}/executions/{execution}"
    result = api("GET", url, params={"view": "FULL"})
    assert json.loads(result["argument"])["trigger"] == "cloud_scheduler"
    started = datetime.fromisoformat(result["startTime"].replace("Z", "+00:00"))
    triggered = datetime.fromisoformat(report["scheduler_triggered_at"])
    assert started >= triggered
    report.setdefault("verification_history", []).append({key: report.get(key) for key in ("status", "error", "finished_at")})
    report.update(status="running", production_execution=result["name"], resumed_at=datetime.now(timezone.utc).isoformat())
    report.pop("error", None)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    deadline = time.monotonic() + 3600
    while result["state"] in ("ACTIVE", "QUEUED"):
        assert time.monotonic() < deadline
        time.sleep(5)
        result = api("GET", url, params={"view": "FULL"})
    assert result["state"] == "SUCCEEDED", result
    report.update(verify_production_result(result, storage.Client(project=PROJECT), api))
    report.update(status="success", finished_at=datetime.now(timezone.utc).isoformat())
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "success", "resumed_existing_execution": execution, "report": str(path)}), flush=True)


def main(production=False):
    root = Path("artifacts/operations") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root.mkdir(parents=True)
    report = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(), "cases": {}}
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(credentials)
    gcs = storage.Client(project=PROJECT)
    remote = gcs.bucket(f"{PROJECT}-drills")
    key = "operations/locks/pipeline-guard-drill.json"

    def save():
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    def api(method, url, **kwargs):
        response = session.request(method, url, timeout=120, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    def executions(workflow):
        return f"https://workflowexecutions.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/workflows/{workflow}/executions"

    def start(case, mode="healthy", workflow=DRILL):
        result = api("POST", executions(workflow), json={"argument": json.dumps({"mode": mode})})
        report["cases"][case] = {"execution": result["name"], "status": "running"}
        save()
        return result["name"]

    def wait(name, expected="SUCCEEDED", timeout=600):
        deadline = time.monotonic() + timeout
        while True:
            result = api("GET", "https://workflowexecutions.googleapis.com/v1/" + name)
            if result["state"] not in ("ACTIVE", "QUEUED"):
                assert result["state"] == expected, result
                return result
            assert time.monotonic() < deadline, "Inspect recorded workflow before retrying"
            time.sleep(3)

    def lock_for(name):
        deadline = time.monotonic() + 90
        while True:
            blob = remote.get_blob(key, timeout=120)
            if blob:
                value = json.loads(blob.download_as_bytes(if_generation_match=int(blob.generation), checksum="crc32c", timeout=120))
                assert value["owner"] == name.split("/")[-1]
                return value, int(blob.generation)
            assert time.monotonic() < deadline, "Lock was not acquired"
            time.sleep(1)

    try:
        assert remote.get_blob(key, timeout=120) is None, "Inspect and explicitly recover the existing drill lock before a new suite"
        print("Checking isolated contention", flush=True)
        owner = start("contention_owner")
        _, generation = lock_for(owner)
        contender = start("contention_skipped")
        result = wait(contender)
        assert json.loads(result["result"])["status"] == "skipped_lock_held"
        report["cases"]["contention_skipped"].update(status="passed", result=result)
        result = wait(owner)
        assert json.loads(result["result"])["status"] == "success"
        assert remote.get_blob(key, timeout=120) is None
        report["cases"]["contention_owner"].update(status="passed", generation=generation, result=result)
        save()
        print("Checking failure retention and recovery", flush=True)
        failed = start("failure_retained", "fail")
        result = wait(failed, "FAILED")
        assert "DRILL: guarded pipeline failure" in json.dumps(result)
        _, generation = lock_for(failed)
        blocked = start("failed_lock_blocks")
        blocked_result = wait(blocked)
        assert json.loads(blocked_result["result"])["status"] == "skipped_lock_held"
        assert int(remote.get_blob(key, timeout=120).generation) == generation
        recovery = release_retained_lock("drill", failed.split("/")[-1], generation, gcs, lambda url: api("GET", url))
        report["cases"]["failure_retained"].update(status="passed", result=result, generation=generation, recovery=recovery)
        report["cases"]["failed_lock_blocks"].update(status="passed", result=blocked_result)
        save()
        print("Checking cancellation retention", flush=True)
        cancelled = start("cancelled_retained")
        _, generation = lock_for(cancelled)
        api("POST", "https://workflowexecutions.googleapis.com/v1/" + cancelled + ":cancel", json={})
        result = wait(cancelled, "CANCELLED")
        assert int(remote.get_blob(key, timeout=120).generation) == generation
        recovery = release_retained_lock("drill", cancelled.split("/")[-1], generation, gcs, lambda url: api("GET", url))
        report["cases"]["cancelled_retained"].update(status="passed", result=result, recovery=recovery)
        save()
        recovered = start("healthy_recovery")
        result = wait(recovered)
        assert json.loads(result["result"])["status"] == "success" and remote.get_blob(key, timeout=120) is None
        report["cases"]["healthy_recovery"].update(status="passed", result=result)
        save()
        print("Waiting for actual Monitoring -> Pub/Sub alert delivery", flush=True)
        subscription = f"https://pubsub.googleapis.com/v1/projects/{PROJECT}/subscriptions/pbmc-operations-alert-inbox"
        deadline = time.monotonic() + 900
        observed = []
        while True:
            page = api("POST", subscription + ":pull", json={"maxMessages": 20, "returnImmediately": True})
            matched = None
            for received in page.get("receivedMessages", []):
                payload = json.loads(base64.b64decode(received["message"]["data"]))
                record = {"message_id": received["message"]["messageId"], "notification": payload}
                if record not in observed:
                    observed.append(record)
                text = json.dumps(payload)
                if failed.split("/")[-1] in text and "PBMC pipeline failure" in text:
                    matched = record
                    api("POST", subscription + ":acknowledge", json={"ackIds": [received["ackId"]]})
                    break
            if matched:
                report["alert_delivery"] = {"status": "passed", **matched, "test_notification_acknowledged": True}
                save()
                break
            report["alert_notifications_observed"] = observed
            save()
            assert time.monotonic() < deadline, "Alert was not observed; inspect policy/channel/logs, do not claim delivery"
            time.sleep(10)
        if production:
            print("Triggering the configured Scheduler job for one production acceptance run", flush=True)
            bucket = gcs.bucket(f"{PROJECT}-data")
            prod_key = "operations/locks/production-pipeline.json"
            assert bucket.get_blob(prod_key, timeout=120) is None
            before = {row["name"] for row in api("GET", executions(PRODUCTION), params={"pageSize": 100}).get("executions", [])}
            api("POST", f"https://cloudscheduler.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/jobs/pbmc-weekly-pipeline:run", json={})
            report["scheduler_triggered_at"] = datetime.now(timezone.utc).isoformat()
            save()
            deadline = time.monotonic() + 600
            while True:
                rows = api("GET", executions(PRODUCTION), params={"pageSize": 100}).get("executions", [])
                created = [row for row in rows if row["name"] not in before]
                if created:
                    assert len(created) == 1
                    report["production_execution"] = created[0]["name"]
                    save()
                    scheduled = api("GET", "https://workflowexecutions.googleapis.com/v1/" + created[0]["name"], params={"view": "FULL"})
                    assert json.loads(scheduled["argument"])["trigger"] == "cloud_scheduler"
                    break
                assert time.monotonic() < deadline, "Scheduler did not create an execution"
                time.sleep(3)
            result = wait(scheduled["name"], timeout=3600)
            report.update(verify_production_result(result, gcs, api))
        report.update(status="success", finished_at=datetime.now(timezone.utc).isoformat())
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}", finished_at=datetime.now(timezone.utc).isoformat())
        save()
        raise
    save()
    print(json.dumps({"status": "success", "report": str(root / 'report.json')}, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production", action="store_true", help="Also run the real configured Scheduler trigger and full pipeline once")
    parser.add_argument("--resume-production", type=Path, help="Verify an existing scheduled execution after completed isolated checks; never retrigger")
    parser.add_argument("--execution", help="Exact existing production workflow UUID for resume")
    args = parser.parse_args()
    if args.resume_production:
        resume_production(args.resume_production, args.execution)
    else:
        main(production=args.production)
