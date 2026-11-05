# Local knowledge generator (Stage 5.2)

The generator reads one explicit QC release, one successful cloud dbt run and
two release-specific BigQuery marts. It writes a draft JSON knowledge package
and a readable Markdown report locally. It does not create BigQuery jobs, mutate
cloud data, upload outputs or change the deployed workflow.

## Inputs and checks

- Explicit project, bucket, full release ID and 32-character cloud dbt run ID.
  There is no latest-run lookup and no use of the local `current.json` pointer.
- The existing release reader restores and validates all four release outputs,
  checks their SHA-256 values and checks the release identity. The original
  manifest supplies the dataset description and QC policy.
- GCS evidence downloads are generation-pinned with CRC32C checks. The output
  records SHA-256 values for the manifest and dbt audit/artifacts.
- The dbt audit must be successful and match the selected inputs and manifest
  checksum. The manifest and results must share an invocation and contain
  successful results for all their expected model and test nodes; their summary
  must agree with the audit result.
- BigQuery reads use the table-data API, not SQL query jobs. Read at most two
  aggregate rows and five exclusion rows to detect unexpected cardinality. The
  expected result is one aggregate row and four distinct exclusion categories.
- Mart identities must match the release and recorded QC policy. Basic totals,
  pass fraction and exclusion counts reconcile with the release summary. The
  output must pass the JSON Schema and resolve every generated evidence reference.

This is generation with defensive input checks, not the independent semantic
validator in Stage 5.3. In particular, this step does not independently recompute
all analytics from the cell flags, check the cohort contents or establish that
the current BigQuery rows still equal the earlier dbt invocation's output.
The mutable marts are read separately, without a cross-table snapshot. The draft
must not be consumed as verified scientific context. No LLM is used to generate
facts or the report.

## Run manually in VS Code PowerShell

Use the project virtual environment and its existing Application Default
Credentials. Install `requirements-knowledge.txt` if these dependencies are not
already present. Run from the repository root:

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m pbmc_platform.generate_knowledge `
  --project pbmc-okf-portfolio `
  --bucket pbmc-okf-portfolio-data `
  --release-id <full-release-id> `
  --dbt-run-id <cloud-dbt-run-id>
```

Replace the angle-bracket placeholders before running. The cloud dbt run ID is
the audit's `run_id`, not the dbt `invocation_id` or Cloud Run execution name.
It identifies `runs/cloud_dbt/<run_id>.json` and `runs/dbt/<run_id>/` in GCS.
`--location`, `--analytics-base` and `--output-dir` are optional; their defaults
are `europe-west1`, `pbmc_analytics` and `artifacts/knowledge`.

The caller needs read access to the selected GCS objects, analytics dataset
metadata and table data. No service account keys or broader cloud write grants
are needed for this command.

## Output

```text
artifacts/knowledge/<release_id>/<dbt_invocation_id>/
  knowledge.json
  report.md
```

The CLI prints both absolute output paths. Open `report.md` in VS Code and use
**Ctrl+Shift+V** for the Markdown preview. It includes identity, dataset facts,
policy, results, dbt summary, limitations and all evidence references/checksums.

Both files are built in a temporary sibling directory and the completed directory
is renamed into place. A repeated run with identical bytes reuses the output.
If the existing files differ, generation fails rather than overwriting them;
choose another output root to retain a changed draft. This is local reproducibility,
not the cloud publication protocol planned for Stage 5.4. Generated outputs stay
under the already ignored `artifacts/` directory by default.

## Checks

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_contract.py tests/test_generate_knowledge.py -q
```

Offline tests exercise actual release restoration through the fake GCS service,
source mapping, deterministic outputs, conflict preservation and rejection of
missing/corrupt evidence, failed dbt tests, invocation mismatches, wrong releases,
changed totals and duplicate exclusion categories. Cloud deployment is Stage 5.5.

## First live generation

On 2026-11-03, the generator read the existing full-pipeline release in
`pbmc-okf-portfolio-data` using cloud dbt run
`613751394ba040fa85925f7fe0271f90`. Its dbt invocation was
`69dc59ca-3a03-4c24-ae95-413c55044480`.

The local package and report are under:

```text
artifacts/knowledge/71a4bb4d026366b644c4aaf8bd4b29d8f8f87fe1701ba2e8d6a88ba164b83f7d/69dc59ca-3a03-4c24-ae95-413c55044480/
```

The saved JSON passed schema validation and reproduced the report byte for byte.
It records 2,700 cells, 2,638 passing, 62 failing, 32,738 features, six successful
dbt models and 30 passing dbt tests. It is still a draft awaiting Stage 5.3.

Stage 5.3 now provides [independent validation](012-knowledge-validation.md).
The original draft remains unchanged; the successful validation produces a
separate verified package and its audit.
