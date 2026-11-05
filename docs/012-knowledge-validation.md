# Independent knowledge validation (Stage 5.3)

The validator checks a draft against the selected cloud release and the current
warehouse data. It writes a separate verified package, readable report and
validation audit only when all checks pass. It never updates the input draft or
writes to GCS/BigQuery. No SQL jobs or LLM calls are used.

## Checks

1. Parse strict JSON, rejecting duplicate keys and non-finite constants. Check
   the schema, explicit release ID, draft status, required evidence kinds and
   section references. Bind evidence to the caller-selected bucket/project,
   release and cloud dbt run. Reject placeholder hashes and changed limitations.
2. Download every artifact with a generation precondition and CRC32C check;
   verify its recorded SHA-256. Restore all four committed release outputs,
   verify the identity hash and reconcile Parquet with its release summary.
   Optional dataset/policy artifact references must also match the configuration
   embedded in the release.
3. Independently calculate cell totals, detected genes, mitochondrial counts and
   percentages from `counts.h5ad`. Compare matrix axes and measurements with
   Parquet, recalculate policy flags, check exclusion reasons and their union,
   and derive totals, pass fraction, feature counts and exclusions. This path
   does not call the producer's `compute_qc` or generator's `assemble_package`.
4. Check dataset descriptions, recorded policy and every reported package result
   against the source data. Confirm manifest/results invocation IDs and every
   expected dbt model/test result, destination project/dataset and the successful
   audit's input manifest and result summary.
5. Read and compare all rows in the three raw BigQuery tables with verified
   Parquet. Compare all aggregate mart fields, all four exclusion rows and every
   cohort row with independently derived expectations. Row order is immaterial;
   extra/missing members, changed values and duplicate members fail validation.
6. Recheck GCS object generations and BigQuery table version metadata at the end.
   If any observed source changes, fail rather than promote the draft.

Pass-fraction and cell-measurement comparisons use absolute tolerance `1e-12`.
SQL mean comparisons additionally allow relative tolerance `1e-12` for distributed
aggregation rounding. For example, the live mean counts per cell differed from
the independently calculated value by only `2.7284841053187847e-12` at magnitude
`2366.9`; this is accepted, while meaningful changes still fail. These tolerances
are recorded in each validation audit.
Integer counts and cohort/raw values must match exactly. A cell may fail multiple
criteria, so exclusion counts are checked individually and their union is checked
against cell eligibility; their sum is not assumed to equal the failed-cell count.

## Run in PowerShell

Use the same virtual environment, dependencies and ADC as the generator:

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m pbmc_platform.validate_knowledge `
  --package-path <path-to-draft-knowledge.json> `
  --project pbmc-okf-portfolio `
  --bucket pbmc-okf-portfolio-data `
  --release-id <full-release-id> `
  --dbt-run-id <cloud-dbt-run-id>
```

Replace the placeholders. The run ID identifies the cloud audit, not the dbt
invocation or Cloud Run execution name. Optional defaults are `--raw-dataset
pbmc_raw`, `--analytics-base pbmc_analytics`, `--location europe-west1` and
`--output-dir artifacts/knowledge_validation`.

The caller needs object read access and metadata/data read access to the raw and
analytics datasets. Broader cloud write permissions are not needed.

## Output and failures

Each attempt gets a unique validation ID:

```text
artifacts/knowledge_validation/<validation_id>/
  validation.json
  knowledge.json   # success only, package_status: verified
  report.md        # success only, rendered from the verified JSON
```

On failure the process exits nonzero and prints the audit path. The failure audit
records the error and checks already passed; it contains no promoted package.
On success the audit records the input hash, validator/schema hashes, selected
resources, timestamps, checked source generations, table versions, metrics and
output hashes. The three files are staged and renamed into place together.
The source draft remains unchanged. Revalidation creates another audit rather
than replacing a previous verification result.

The verified package retains the scientific limitations and removes only the
draft-specific pending-validation sentence. A manually edited `verified` label
does not establish provenance; consumers must use the trusted validation and
[controlled publication path](013-knowledge-publication.md). The validator accepts drafts, not self-labelled
verified packages.

## Verification boundary

Verification records the evidence observed during this attempt. Per-object
generation preconditions and table version rechecks detect observed changes;
they do not create a cross-service transaction, a warehouse snapshot or a lock.
Run one pipeline operation per release at a time. Future modifications require
fresh validation. Historical dbt artifacts attest their recorded invocation;
current raw/mart rows are separately checked against the release.

This step verifies computational consistency within the one-donor dataset. It
does not validate biological conclusions or add cell-type/disease inference.
Durable cloud publication is implemented in [Step 5.4](013-knowledge-publication.md).
Workflow integration remains Step 5.5.

## Offline tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_validate_knowledge.py -q
```

The tests cover successful promotion, unchanged draft/source objects, complete
cohort comparison, report hashes, altered totals/policy/description, incorrect
references, checksum errors, failed dbt tests, raw/mart tampering, missing cohort
members, duplicate exclusions and source changes during validation. Separate
recomputation tests reject matrix/measurement and policy/flag disagreement.

## First live verification

On 2026-11-04, validation `5fb837e770ea40198dd46018ae3acd26` succeeded for
release `71a4bb4d026366b644c4aaf8bd4b29d8f8f87fe1701ba2e8d6a88ba164b83f7d`
and cloud dbt run `613751394ba040fa85925f7fe0271f90`.

All six validation groups passed, covering eight GCS objects and six BigQuery
tables. Independent results were 2,700 total cells, 2,638 passing, 62 failing,
32,738 features, 13 mitochondrial features and 6,390,631 counts. The exact cohort
contained 2,638 rows; dbt evidence recorded six successful models and 30 passing
tests. Verified outputs are in
`artifacts/knowledge_validation/5fb837e770ea40198dd46018ae3acd26/`.

The initial strict-mean attempt stopped without promoting a package; its failed
audit is retained under validation `3e86c37f598243bdad32d8e8ec38471c`.
After measuring the rounding difference and adding regression coverage for the
documented mean tolerance, the live recheck passed. The final full local suite
passed all 155 tests.
