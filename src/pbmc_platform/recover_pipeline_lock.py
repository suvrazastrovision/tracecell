"""Release one explicitly selected retained lock only after quiescence checks."""

import argparse
import json
import re


PROJECT, REGION = "pbmc-okf-portfolio", "europe-west1"
TARGETS = {
    "production": (f"{PROJECT}-data", "operations/locks/production-pipeline.json", "pbmc-qc-workflow",
                   ("pbmc-qc", "pbmc-warehouse-load", "pbmc-dbt-build", "pbmc-knowledge-build")),
    "drill": (f"{PROJECT}-drills", "operations/locks/pipeline-guard-drill.json", "pbmc-pipeline-guard-drill", ()),
}


def release_retained_lock(target, owner, generation, storage_client=None, api=None):
    if target not in TARGETS or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", owner):
        raise ValueError("Choose an approved target and exact workflow execution UUID")
    if type(generation) is not int or generation <= 0:
        raise ValueError("Expected a positive, explicitly selected lock generation")
    if storage_client is None:
        from google.cloud import storage
        storage_client = storage.Client(project=PROJECT)
    if api is None:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        session = AuthorizedSession(credentials)

        def api(url):
            response = session.get(url, timeout=120)
            response.raise_for_status()
            return response.json()

    bucket, key, workflow, jobs = TARGETS[target]
    blob = storage_client.bucket(bucket).get_blob(key, timeout=120)
    if blob is None or int(blob.generation) != generation:
        raise ValueError("Lock is missing or its generation changed; inspect again")
    lock = json.loads(blob.download_as_bytes(if_generation_match=generation, checksum="crc32c", timeout=120))
    if lock.get("schema_version") != 1 or lock.get("owner") != owner or lock.get("workflow") != workflow:
        raise ValueError("Lock owner or workflow differs from the selected recovery")
    execution = api(f"https://workflowexecutions.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/workflows/{workflow}/executions/{owner}")
    if execution["state"] not in ("SUCCEEDED", "FAILED", "CANCELLED"):
        raise ValueError("Lock owner is not terminal; do not release")
    checked = []
    for job in jobs:
        url = f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/{job}/executions?pageSize=100"
        while True:
            page = api(url)
            for row in page.get("executions", []):
                if not row.get("completionTime"):
                    raise ValueError(f"Cloud Run execution may still be active: {row['name']}")
                checked.append(row["name"])
            if not page.get("nextPageToken"):
                break
            from urllib.parse import quote
            url = url.split("&pageToken=")[0] + "&pageToken=" + quote(page["nextPageToken"], safe="")
    # The generation precondition protects against another operator replacing the lock.
    blob.delete(if_generation_match=generation, timeout=120)
    return {"status": "released", "target": target, "owner": owner, "generation": generation,
            "owner_state": execution["state"], "completed_job_executions_checked": len(checked)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--generation", type=int, required=True)
    try:
        print(json.dumps(release_retained_lock(**vars(parser.parse_args())), indent=2))
    except Exception as error:
        parser.exit(1, f"Lock recovery refused or failed: {error}\n")


if __name__ == "__main__":
    main()
