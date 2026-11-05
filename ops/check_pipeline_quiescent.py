"""Read-only precondition for introducing the production pipeline guard."""
import json
from pathlib import Path
from urllib.parse import quote
import google.auth
from google.auth.transport.requests import AuthorizedSession

PROJECT, REGION = "pbmc-okf-portfolio", "europe-west1"


def check():
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(credentials)
    checked = {}
    sources = {"workflow": (f"https://workflowexecutions.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/workflows/pbmc-qc-workflow/executions?pageSize=100", "executions")}
    for job in ("pbmc-qc", "pbmc-warehouse-load", "pbmc-dbt-build", "pbmc-knowledge-build"):
        sources[job] = (f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/{job}/executions?pageSize=100", "executions")
    for name, (url, field) in sources.items():
        count = 0
        while True:
            response = session.get(url, timeout=120)
            response.raise_for_status()
            page = response.json()
            for execution in page.get(field, []):
                if name == "workflow":
                    assert execution["state"] in ("SUCCEEDED", "FAILED", "CANCELLED"), execution["name"]
                else:
                    assert execution.get("completionTime"), execution["name"]
                count += 1
            if not page.get("nextPageToken"):
                break
            url = url.split("&pageToken=")[0] + "&pageToken=" + quote(page["nextPageToken"], safe="")
        checked[name] = count
    return {"status": "quiescent", "completed_executions_checked": checked}


if __name__ == "__main__":
    result = check()
    Path("artifacts/pipeline_quiescent.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
