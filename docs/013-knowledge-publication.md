# Verified knowledge publication (Stage 5.4)

The publisher checks a trusted local validator bundle and uploads it to the
existing private pipeline bucket. It preserves the exact verified bytes and
creates a completion marker after all three files pass remote readback checks.
It does not regenerate analytics or change the deployed workflow.

## Published layout and identity

```text
gs://<bucket>/knowledge/releases/<release_id>/<package_id>/
  knowledge.json
  report.md
  validation.json
  publication.json

gs://<bucket>/runs/knowledge_publication/<attempt_id>.json
```

The full release ID identifies the QC release. The package ID is SHA-256 of
canonical JSON describing the dataset/release, validation ID, dbt run/invocation
and the SHA-256/byte length of each of the three verified files. The successful
validation audit is part of this identity. A new validation produces a separate
version even when the numerical results are unchanged. There is no latest pointer.

`publication.json` is the completion marker. It contains publication schema
version 1, the identity, package ID and each file's URI, generation, checksum and
byte length. Readers must require this marker, verify its identity hash and read
the recorded generations/checksums; a prefix containing only some files is not
a completed publication.

## Local checks and trust

The publisher checks the package schema and verified status, selected destination
and release, all six successful validation groups, current validator/schema code
hashes, output checksums, reproducible report, scientific limitations, metrics,
exclusions, cohort count, dbt invocation and evidence bindings to the recorded
source generations and tables. If validator code or schema has changed, run
validation again before publishing.

This is a publisher for trusted outputs from this project's validator. A local
audit is not a cryptographic signature and is not a mechanism for authenticating
arbitrary third-party JSON. Consumers use the controlled publication path. The
uploaded audit preserves the evidence observed at validation time; publication
does not assert that mutable warehouse tables have remained unchanged since then.

The publisher captures all three local files once and checks this snapshot before
upload. Later edits to the originals cannot change the bytes being published.

## Writes, retries and failure behavior

Every upload uses `if_generation_match=0` and CRC32C. On an already existing object,
the publisher reads its actual generation-pinned bytes and compares SHA-256. It
reuses identical objects, rejects conflicting bytes and never deletes or replaces
an object. The completion marker is written last among the bundle objects and
receives the same readback verification.

After a partial upload, rerun the same command. Existing correct files retain
their generations while missing objects are filled in. A fully repeated run
returns `reused: true` and creates a separate attempt audit. Conflicting objects
require investigation; retry does not remove them or silently overwrite them.

Each attempt records a local audit under
`artifacts/knowledge_publication_runs/<attempt_id>.json`; attempts that reach GCS
also try to publish their audit under `runs/knowledge_publication/`. If the bundle
commits but the attempt audit upload fails, the command fails and preserves the
local error record. Retry verifies/reuses the bundle and publishes a new attempt
audit. The published validation audit already remains inside the committed bundle.
There is no cross-object transaction or rollback.

The application uses create-only writes. This is not a bucket retention lock;
an administrator with broader permissions can still modify or delete objects.

## Run in PowerShell

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m pbmc_platform.publish_knowledge `
  --validation-dir <successful-validation-directory> `
  --project pbmc-okf-portfolio `
  --bucket pbmc-okf-portfolio-data `
  --release-id <full-release-id> `
  --dry-run
```

Replace the placeholders. Dry run validates local inputs and prints the exact
destination without cloud calls. Remove `--dry-run` to publish. Existing ADC is
used. The writer needs object create/get access for the knowledge and attempt
audit prefixes; no public access or service account keys are required. Cloud Run
service account provisioning belongs to Stage 5.5.

The result prints `publication_uri`, package ID, object generations/checksums,
whether the bundle was reused, and local/remote attempt audit locations. The
human-readable report remains a Markdown file that can be downloaded and opened
with VS Code's **Ctrl+Shift+V** preview.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_publish_knowledge.py -q
```

Protocol tests cover ordered commit, exact readback, unchanged generations on
replay, interruption at each file, conflicting remote bytes, changed source files
after snapshot, invalid local evidence, dry run, a new validation version, and
an audit-upload failure after commit followed by successful retry.

## First live publication

On 2026-11-04, the verified validation bundle
`5fb837e770ea40198dd46018ae3acd26` was published to `pbmc-okf-portfolio-data`.
The release ID is
`71a4bb4d026366b644c4aaf8bd4b29d8f8f87fe1701ba2e8d6a88ba164b83f7d`
and the package ID is
`927521cc8ee04e1621748f415f27e25b9cab5deb03e8014447fe5ffd8d3dc0aa`.

The first attempt created the three files and completion marker. A second attempt
returned `reused: true`; all four bundle generations remained unchanged. An
independent download verified the marker identity, every file's generation,
SHA-256 and byte length, exact agreement with the local verified files, and both
durable successful attempt audits. The local source bundle remained unchanged.

- First attempt: `runs/knowledge_publication/3b1a9204dfa74b879f3002c519938176.json`
- Replay: `runs/knowledge_publication/9fd137fd0fb74c87a051821176d7b8e8.json`
- Completion marker generation: `1793819021028565`
- Marker SHA-256: `86c943059eefb214d0f0c87ef921d75b76710b741df01e3456138f5b9b84572f`
- Local proof: `artifacts/knowledge_publication_verification.json`

The full local suite at this stage passed all 176 tests.
[Step 5.5](014-cloud-knowledge.md) now adds the Cloud Run worker and integrates
generation, validation and publication into the workflow.
