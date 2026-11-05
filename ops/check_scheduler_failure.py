"""Verify actual Scheduler failure logs and notification delivery using a paused drill."""

import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import google.auth
from google.auth.transport.requests import AuthorizedSession

from ops.run_drills import read_logs


def main():
    project, region = "pbmc-okf-portfolio", "europe-west1"
    name = "pbmc-scheduler-failure-drill"
    missing = "pbmc-scheduler-missing-target-drill"
    job_url = f"https://cloudscheduler.googleapis.com/v1/projects/{project}/locations/{region}/jobs/{name}"
    report = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat()}
    path = Path("artifacts/scheduler_failure_verification.json")
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(credentials)

    def api(method, url, **kwargs):
        response = session.request(method, url, timeout=120, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    def save():
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    resumed = False
    try:
        job = api("GET", job_url)
        assert job["state"] == "PAUSED"
        assert job["httpTarget"]["uri"].endswith(f"/workflows/{missing}/executions")
        assert job["httpTarget"]["oauthToken"]["serviceAccountEmail"] == f"pbmc-scheduler@{project}.iam.gserviceaccount.com"
        response = session.get(f"https://workflows.googleapis.com/v1/projects/{project}/locations/{region}/workflows/{missing}", timeout=120)
        assert response.status_code == 404, "Missing target unexpectedly exists; do not dispatch"
        report["missing_target_confirmed"] = True
        report["previous_attempt"] = job.get("lastAttemptTime")
        # Scheduler rejects run-now for a paused job. Enable this missing-target
        # drill briefly; its cron occurrence is in the future, then restore pause.
        api("POST", job_url + ":resume", json={})
        resumed = True
        api("POST", job_url + ":run", json={})
        save()
        print("Waiting for the paused drill's forced Scheduler attempt", flush=True)
        deadline = time.monotonic() + 900
        while True:
            job = api("GET", job_url)
            if job.get("lastAttemptTime") and job["lastAttemptTime"] != report["previous_attempt"] and job.get("status", {}).get("code", 0) != 0:
                break
            assert time.monotonic() < deadline, "Drill attempt did not finish with an error"
            time.sleep(5)
        assert job["state"] == "ENABLED"
        report["scheduler"] = job
        query = (f'resource.type="cloud_scheduler_job" AND resource.labels.job_id="{name}" '
                 f'AND timestamp>="{report["started_at"]}" AND severity>=ERROR')
        deadline = time.monotonic() + 300
        while True:
            logs = read_logs(api, query)
            if any("404" in json.dumps(row) for row in logs):
                report["expected_404_logs"] = logs
                break
            assert time.monotonic() < deadline, "Expected missing-target 404 was not observed"
            time.sleep(5)
        print("Missing-target 404 confirmed; waiting for private alert delivery", flush=True)
        subscription = f"https://pubsub.googleapis.com/v1/projects/{project}/subscriptions/pbmc-operations-alert-inbox"
        deadline = time.monotonic() + 900
        while True:
            page = api("POST", subscription + ":pull", json={"maxMessages": 20, "returnImmediately": True})
            matched = None
            for row in page.get("receivedMessages", []):
                value = json.loads(base64.b64decode(row["message"]["data"]))
                text = json.dumps(value)
                if name in text and "PBMC Scheduler delivery failure" in text:
                    matched = {"message_id": row["message"]["messageId"], "notification": value}
                    api("POST", subscription + ":acknowledge", json={"ackIds": [row["ackId"]]})
                    break
            if matched:
                report["alert_delivery"] = {"status": "passed", **matched, "test_notification_acknowledged": True}
                break
            assert time.monotonic() < deadline, "Scheduler failure alert was not delivered"
            time.sleep(10)
        report["status"] = "success"
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if resumed:
            api("POST", job_url + ":pause", json={})
            final_job = api("GET", job_url)
            assert final_job["state"] == "PAUSED"
            report["restored_paused"] = True
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
    print(json.dumps({"status": "success", "drill_remains_paused": True, "report": str(path)}), flush=True)


if __name__ == "__main__":
    main()
