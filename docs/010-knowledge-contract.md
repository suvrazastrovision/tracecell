# PBMC knowledge package contract v1

Stage 5.1 defines the interface for the generator and validator. Step 5.2 now
generates local draft packages; Step 5.3 independently validates them.
Step 5.4 publishes the verified bundle. See [the generator guide](011-knowledge-generator.md),
[validation guide](012-knowledge-validation.md) and [publication guide](013-knowledge-publication.md).

## Purpose and files

This is a project-defined JSON knowledge package, called OKF in this repository.
It does not claim compatibility with an external Open Knowledge Format standard.
Its intended consumer is a scientist-facing assistant explaining the dataset,
QC policy, results and limitations with source references.

- Schema: [pbmc-knowledge-v1.schema.json](../schemas/pbmc-knowledge-v1.schema.json)
- Example: [pbmc-knowledge-v1.json](../examples/pbmc-knowledge-v1.json)

The schema uses JSON Schema Draft 2020-12. The exact supported schema version is
`1.0.0`; unknown fields are rejected. A future incompatible contract needs a new
schema and an explicit consumer upgrade. `format_name` is `pbmc-project-knowledge`.

The example has `package_status: illustrative`. Its dataset totals reflect the
current PBMC release, but its example bucket, project, invocation ID and
zero-filled artifact checksums are placeholders. They are not cloud evidence.
Schema validity alone must never promote an illustrative package to verified.

The generator uses `package_status: draft` for source-derived packages awaiting
Step 5.3 validation. This status is distinct from the fictional illustrative
example and from a verified, trusted publication.

## Fields and source mappings

| Section | Meaning and authoritative source |
| --- | --- |
| `identity` | Dataset ID/version and full release SHA-256 from the selected `release.json`. |
| `dataset` | Organism, tissue, donor count, reference genome, input checksum, source URLs, licence and attribution from `release.identity.dataset_manifest`. `original_source_url` maps to its `original_source` field. |
| `qc_policy` | Policy ID/version, inclusive minimum genes, exclusive maximum genes and mitochondrial percentage, and mitochondrial prefix from `release.identity.qc_policy`. Do not substitute the current working-tree configuration for the recorded policy. |
| `results.metrics` | `cells_total`, `cells_pass`, `cells_fail`, `features_total`, `total_counts`, `mitochondrial_features` and `qc_pass_fraction` from the release-specific `mart_dataset_qc`. |
| `results.exclusions.counts` | The four exclusion reasons and their `excluded_cells` values from `mart_qc_exclusions`. |
| `dbt` | Successful invocation ID, model count and test count from the matching cloud dbt audit, `manifest.json` and `run_results.json`. |
| `evidence` | Named sources referenced by each section's `evidence_refs`. Artifact entries contain a GCS URI and SHA-256; BigQuery entries contain a fully qualified table ID. |
| `limitations` | Mandatory scientific boundaries plus relevant explanatory limitations. No generated biological conclusions. |

The release manifest is sufficient evidence for its embedded dataset configuration
and QC policy. Separate configuration/policy artifact entries are optional and
must actually exist if referenced by a verified package. The example shows this
optional form using explicitly fictional artifact locations.

Every result group has nonempty evidence references. Individual metric names map
to the identically named columns in `mart_dataset_qc`; exclusion keys map to the
`exclusion_reason` rows in `mart_qc_exclusions`. A future answer should cite the
relevant evidence and release ID, rather than treating the package as independent
scientific evidence.

## Validation contract

There are two gates. The JSON Schema implements structural checks now. The
source-aware validator in Step 5.3 implements the semantic checks below.

### Structural gate

Reject missing fields, unknown fields, unsupported versions, malformed lowercase
64-character release IDs or checksums, negative counts, fractions outside [0, 1],
empty evidence reference lists, malformed evidence entries and invalid enums.
Require all four exclusion categories and `reasons_may_overlap: true`.

### Semantic gate for verified packages

1. Resolve every evidence reference. Require release-manifest, aggregate-table,
   exclusion-table and all three dbt artifact/audit sources in their relevant
   sections. Check their kinds as well as their IDs.
2. Download evidence using pinned object generations and verify its SHA-256.
   Recompute the selected release identity hash and validate the committed release
   with the existing release-validation machinery. Reject example placeholders.
3. Require exact dataset and policy identity agreement across the package,
   release, dbt audit, manifest/results and both BigQuery marts. Require the table
   datasets to identify the selected full release ID. No implicit latest release.
4. Match dbt invocation IDs across manifest and results; require every expected
   model and test to have completed successfully, using the existing dbt result
   verification behavior. Validate its successful audit and published artifacts.
   Six models and 30 tests describe the current build, not a permanent constant.
5. Require one aggregate row and exactly one row for each exclusion category.
   Verify package values against those rows and reconcile the corresponding
   counts against the verified release summary. Verify cohort count equals
   `cells_pass`. Use read-only source access.
6. Require `cells_total = cells_pass + cells_fail`, positive total cells/features,
   `mitochondrial_features <= features_total`, and each exclusion count no larger
   than `cells_fail`. The exclusion union must cover failures, but overlapping
   reasons mean the counts need not sum to `cells_fail`. This requires checking
   verified cell flags, not just adding the four aggregate counts.
7. Require `qc_pass_fraction` to match `cells_pass / cells_total` with absolute
   tolerance `1e-12`. Reject non-finite numbers. Require the minimum gene threshold
   to be smaller than the maximum.
8. Preserve the one-healthy-donor boundary, lack of cell-type inference and lack
   of support for disease comparisons. Schema checks nonempty limitation text;
   the semantic validator checks these required meanings against fixed project
   statements. User-written text alone is insufficient for promotion.

`package_status: verified` is a validator output after both gates succeed; it is
not a trusted assertion from an arbitrary JSON file. Consumers must use the
trusted publication/validation path. The schema cannot check arithmetic,
cross-references, cloud object existence or scientific correctness.

## Reference results and boundaries

For the current release: 2,700 cells, 2,638 passing, 62 failing, 32,738 features,
6,390,631 counts and 13 mitochondrial features. Exclusion counts are zero for
zero counts and low genes, five for high genes and 57 for high mitochondrial
percentage. Future releases must use their own validated values.

V1 contains aggregate facts and provenance. Cell records and the expression
matrix remain in their existing storage locations. The readable companion report
is generated exclusively from the package JSON. Publication, package checksums,
Cloud Run deployment and workflow integration belong to subsequent substeps.

## Local contract checks

Install `requirements-knowledge-dev.txt` into the project virtual environment,
then run from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_contract.py -q
```

These checks validate the schema, example and structural rejection scenarios.
They do not contact GCP or establish that the illustrative package is verified.
