"""Read-only acceptance checks for the IAM-protected Cloud Run assistant."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess

import google.auth
from google.auth.transport.requests import AuthorizedSession
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from pbmc_platform import assistant
from pbmc_platform.evaluate_assistant import DEFAULT_CASES, check_answer
from pbmc_platform.ingestion import sha256_file


PROJECT, REGION, SERVICE = "pbmc-okf-portfolio", "europe-west1", "pbmc-assistant"
IMAGE = "europe-west1-docker.pkg.dev/pbmc-okf-portfolio/pbmc-containers/pbmc-assistant@sha256:837337193ef8e03d43c3c475b596f1873af8d0c16ae76ad7583c34ccfd98e223"
PACKAGE = "e2c908da59111f5e12ebac24804e526f938802a7bbcbd6020bb7cd4cb0a2e04f"


def main():
    report = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat()}
    path = Path("artifacts/cloud_assistant_verification.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        api = AuthorizedSession(credentials)
        retry = Retry(total=2, backoff_factor=1, allowed_methods=frozenset({"GET", "POST"}),
                      status_forcelist=(429, 500, 502, 503, 504))
        api.mount("https://", HTTPAdapter(max_retries=retry))
        web = requests.Session()
        web.mount("https://", HTTPAdapter(max_retries=retry))

        def call(method, url, **kwargs):
            response = api.request(method, url, timeout=120, **kwargs)
            response.raise_for_status()
            return response.json()

        resource = f"projects/{PROJECT}/locations/{REGION}/services/{SERVICE}"
        service = call("GET", f"https://run.googleapis.com/v2/{resource}")
        assert service["terminalCondition"]["state"] == "CONDITION_SUCCEEDED"
        assert service["latestReadyRevision"] == service["latestCreatedRevision"]
        assert not service.get("invokerIamDisabled", False)
        template = service["template"]
        runtime = f"{SERVICE}@{PROJECT}.iam.gserviceaccount.com"
        assert template["serviceAccount"] == runtime
        assert template["containers"][0]["image"] == IMAGE
        assert template["scaling"].get("minInstanceCount", 0) == 0
        assert template["scaling"]["maxInstanceCount"] == 1
        assert template["maxInstanceRequestConcurrency"] == 8
        policy = call("GET", f"https://run.googleapis.com/v2/{resource}:getIamPolicy")
        assert not any(member in ("allUsers", "allAuthenticatedUsers")
                       for binding in policy.get("bindings", []) for member in binding.get("members", []))
        assert any(binding["role"] == "roles/run.invoker" for binding in policy.get("bindings", []))
        project_policy = call("POST", f"https://cloudresourcemanager.googleapis.com/v1/projects/{PROJECT}:getIamPolicy", json={})
        runtime_roles = [binding["role"] for binding in project_policy.get("bindings", [])
                         if f"serviceAccount:{runtime}" in binding.get("members", [])]
        assert runtime_roles == [], runtime_roles
        uri = service["uri"]
        assert uri.startswith("https://") and uri.endswith(".run.app")
        anonymous = {}
        for route in ("/", "/answer.json?question=qc", "/ready"):
            response = web.get(uri + route, timeout=60)
            anonymous[route] = response.status_code
            assert response.status_code in (401, 403), "An anonymous route was not rejected"
        gcloud = str(Path(os.environ["LOCALAPPDATA"]) / "Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd")
        # Keep the short-lived token in memory only; never print or save headers.
        token = subprocess.run([gcloud, "auth", "print-identity-token", "--quiet"],
                               check=True, capture_output=True, text=True).stdout.strip()
        headers = {"Authorization": f"Bearer {token}"}
        response = web.get(uri + "/ready", headers=headers, timeout=120)
        response.raise_for_status()
        assert response.json()["status"] == "ready"
        response = web.get(uri + "/", headers=headers, timeout=120)
        response.raise_for_status()
        assert "Authenticated cloud demo" in response.text and "2,638 of 2,700" in response.text
        assert response.headers["Cache-Control"] == "no-store"
        pin = json.loads(assistant.DEFAULT_PIN.read_text())
        package = assistant.load_package(assistant.DEFAULT_BUNDLE, pin)
        assert pin["package_id"] == PACKAGE
        records = []
        for case in json.loads(DEFAULT_CASES.read_text())["cases"]:
            response = web.get(uri + "/answer.json", params={"question": case["question"]}, headers=headers, timeout=120)
            response.raise_for_status()
            result = response.json()
            assert result["question"] == case["question"]
            for field in ("release_id", "package_id", "publication_uri", "publication_generation", "publication_sha256"):
                assert result[field] == pin[field], f"Cloud publication pin differs: {field}"
            passed = check_answer(result, package, pin, case["expected_topic"])
            assert passed, f"Cloud evidence query failed: {case['question']}"
            records.append({"question": case["question"], "expected_topic": case["expected_topic"],
                            "actual_topic": result["topic"], "evidence_checks_passed": passed})
        revision = call("GET", f"https://run.googleapis.com/v2/{service['latestReadyRevision']}")
        report.update(status="success", project=PROJECT, region=REGION, service=SERVICE, uri=uri,
                      ready_revision=service["latestReadyRevision"], image=IMAGE,
                      revision_containers=revision.get("containers"), runtime_service_account=runtime,
                      runtime_direct_project_roles=runtime_roles, invoker_iam_check_enabled=True,
                      public_invoker_binding=False, anonymous_http_statuses=anonymous,
                      authenticated_health="ready", authenticated_html_verified=True,
                      package_id=PACKAGE, publication_generation=pin["publication_generation"],
                      publication_sha256=pin["publication_sha256"], scaling=template["scaling"],
                      request_concurrency=template["maxInstanceRequestConcurrency"],
                      cases_sha256=sha256_file(DEFAULT_CASES), cloud_cases_passed=len(records), cases=records)
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "uri": report["uri"],
                      "cloud_cases_passed": report["cloud_cases_passed"], "evidence": str(path)}, indent=2))


if __name__ == "__main__":
    main()
