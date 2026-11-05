# Stage 6.4: authenticated Cloud Run assistant

The assistant is a separate Cloud Run **service**, `pbmc-assistant` in
`europe-west1`. The pipeline's four Cloud Run **jobs** remain batch workers.
The service serves the same selected verified package and grammar-routed answers
as the local interface. It does not use a language model.

## Open the deployed app from Windows

The HTTPS endpoint is
`https://pbmc-assistant-gjjq4syn4a-ew.a.run.app`. A direct anonymous browser visit
is rejected. Use Google's authenticated local proxy to view it with your current
gcloud identity, as described in [Google's developer authentication guide](https://docs.cloud.google.com/run/docs/authenticating/developers).

In a VS Code PowerShell terminal:

```powershell
$env:Path += ";$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin"
$env:CLOUDSDK_PYTHON = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
gcloud run services proxy pbmc-assistant `
  --project pbmc-okf-portfolio --region europe-west1 --port 8766
```

Keep that terminal running and open **http://127.0.0.1:8766**. The page should say
**Authenticated cloud demo**. Type **How many cells failed quality control?**
and select **Show answer**. This address forwards requests to the cloud service;
`http://127.0.0.1:8765` is the separate local Python app.

Press Ctrl+C to stop the proxy. Restart it with the same command after closing
VS Code or restarting Windows. The proxy uses short-lived credentials; it does
not make the service public. The account must have invoke permission. The service
has a named user invoker grant; inherited project permissions may also authorize
administrators.

The official proxy component is installed in this SDK. Its first noninteractive
installation required the existing standalone Python path shown above. The SDK
`bin` directory must be on PATH so gcloud can locate `cloud-run-proxy.exe` on
Windows. No service account key or token needs to be pasted or saved.

To inspect the deployment manually, open [the Cloud Run service](https://console.cloud.google.com/run/detail/europe-west1/pbmc-assistant/metrics?project=pbmc-okf-portfolio).
Check Revisions for the image digest and runtime identity, Security/Permissions
for authenticated invocation, and Logs for requests and application status.

## Resources and permissions

| Resource | Purpose |
| --- | --- |
| `google_cloud_run_v2_service.assistant` | HTTPS service with the invoker IAM check enabled |
| `google_service_account.assistant` | Runtime identity `pbmc-assistant@pbmc-okf-portfolio.iam.gserviceaccount.com` |
| `google_cloud_run_v2_service_iam_member.assistant_invoker` | Named service-specific invocation grant |
| Existing Artifact Registry repository | Stores immutable assistant image versions |

The service has internet-reachable ingress protected by IAM; it is not a
VPC-internal endpoint. There are no `allUsers`/`allAuthenticatedUsers` service
bindings. The Terraform variable rejects public principals. Its caller list is
stored in the ignored `infra/assistant.auto.tfvars.json`, rather than putting the
operator's email in committed configuration. On another checkout, configure your
own named invoker members before planning a deployment.

No BigQuery or Storage role is assigned to the runtime account by this deployment.
The image contains `configs/assistant-package.json` and the exact four files in
`examples/verified-pbmc/`. Every answer checks that snapshot locally. A new
pipeline publication does not silently change the app's dataset.

The configuration uses 1 CPU, 1 GiB memory, request-based CPU allocation,
startup CPU boost, concurrency 8, a 30-second request timeout and revision scaling
from zero to one instance. These are demo capacity settings, not measured cost
savings or a hard billing cap. Image storage, requests and logs can incur charges.

## Container contract and readiness

Local mode binds only `127.0.0.1`. Explicit `--cloud-run` mode requires the
expected `K_SERVICE` value and a valid injected `PORT`, and listens on
`0.0.0.0:PORT`, following the [Cloud Run container contract](https://docs.cloud.google.com/run/docs/container-contract).
Cloud Run performs TLS termination, ingress routing and authentication before
requests reach the container; the application does not implement a second login.

The startup probe uses **`/ready`**, which rechecks the selected publication and
bundle. The initial `/healthz` path returned a platform 404 at the service URL,
so it was replaced before acceptance; a frontend 404 was not counted as proof
of application readiness. The corrected image uses a new immutable tag and digest.

The image inherits the pinned scientific runtime and its non-root user, UID
10001. It includes no ADC files or service account keys. The server is a bounded
portfolio demo using Python's threaded HTTP server, not a general production web
platform. Visual layout, large concurrent loads, timeout/cancellation recovery,
alerts and cost measurements are not established by these deployment checks.

## Rebuild, update and verify

Source files are [Dockerfile.assistant](../Dockerfile.assistant), its explicit
allowlist and [infra/assistant.tf](../infra/assistant.tf). A snapshot upgrade
requires selecting a successfully verified publication, preserving its exact
bytes, updating the trust pin, and building a new image tag. Smoke-test it, push
it, update the digest variable, review the full Terraform plan and apply that
saved plan. Reject plans that unexpectedly modify the pipeline or destroy data.
Rollback similarly uses a reviewed Terraform update to a previous accepted
image digest, keeping infrastructure state aligned.

Read-only cloud acceptance command, from the repository root with ADC and gcloud
credentials for the intended caller:

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m ops.verify_cloud_assistant
```

The checker validates the ready revision, image, runtime identity, service IAM,
direct project role assignments, scale settings and anonymous denial on the page,
answer and readiness routes. It then invokes all 60 regression questions with a
short-lived identity token and checks answers against the selected verified
package. Tokens and Authorization headers are never printed or saved. The local
report is `artifacts/cloud_assistant_verification.json`; failures exit nonzero.

## Accepted live results

On **2026-11-04**, revision **`pbmc-assistant-00002-qzw`** passed all **60 cloud
regression cases**: 30 supported queries and 30 unsupported requests. Acceptance
also checked the exact package/publication pin on every response, authenticated
HTML and readiness, and HTTP **403** for anonymous requests to `/`,
`/answer.json?question=qc` and `/ready`. The runtime account had no direct project
IAM roles, the invoker IAM check was enabled and the service policy had no public
principal binding. The authenticated Windows proxy was checked against cloud
HTML, a typed QC answer and `/ready`.

Accepted image:
`europe-west1-docker.pkg.dev/pbmc-okf-portfolio/pbmc-containers/pbmc-assistant@sha256:837337193ef8e03d43c3c475b596f1873af8d0c16ae76ad7583c34ccfd98e223`.
Cloud Run resolves its Linux manifest to
`sha256:14abdd7845eb4da5421b35471aae7845d29401bb995d7ab65d19ffee9b338a95`;
the OCI image index and its platform manifest have different digests by design.

The selected production knowledge package remains
`e2c908da59111f5e12ebac24804e526f938802a7bbcbd6020bb7cd4cb0a2e04f`,
with publication generation `1793822792589953`. The existing pipeline resources
were not changed: initial deployment added three assistant resources; the
readiness correction updated only this service. Final Terraform formatting and
validation passed, and the post-update plan reported **no changes**. All **139
focused assistant tests** passed, and the corrected container passed its local
port/readiness/answer checks as UID 10001.

The [portable cloud evidence summary](../examples/cloud-assistant-evidence.json)
records the actual revision, image, IAM/scaling checks, anonymous statuses and
publication identity. The full report SHA-256 is
`e68e040a4f67fa2dabd81d12c05494ffaab72aaa7d9e656691c1e7eae75c9fa5`.
Stage 6.4 is accepted. Future model integration, broader language evaluation and
additional operational hardening are outside this increment.
