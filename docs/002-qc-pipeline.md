# QC contracts and recovery

## Inputs and reproducibility

`configs/dataset.yaml` pins the Scanpy-hosted PBMC3k AnnData file by SHA-256,
identifies the original 10x dataset, and records dimensions, reference genome,
license, attribution, and count layer. The checksum is the observed checkpoint
1 file checksum; it protects against subsequent content changes. It is not an
independent signature from the original data publisher.

Only sparse, nonnegative, finite, integer-valued unnormalized counts in `X`
are supported. Shape, cell-barcode uniqueness, and original gene-ID uniqueness
must match the contract. Counts happen to be stored as float32 in this source;
QC sums use float64 accumulation and are exported as int64 count metrics.

The initial source is already a filtered gene-barcode matrix. The pipeline's
QC flags are an additional policy applied to those 2,700 cells; excluded
droplets from the original Cell Ranger analysis are unavailable here.

## Cell and gene identity

- Cell ID: `<dataset_id>:<dataset_version>:<original_barcode>`.
- Gene key within a dataset version: the original Ensembl `gene_ids` value.
- `source_feature_name` preserves the source AnnData feature index. Scanpy may
  have added suffixes to make duplicate symbols unique; it is not a new gene ID.
- Each cell row records dataset and policy versions. IDs remain stable when
  only the QC policy changes.
- No cell types or donor-level labels are inferred. The dataset manifest
  records one donor, and cell IDs do not pretend to identify independent donors.

`counts.h5ad` retains all cells, genes, and count values. Its `obs` index is the
stable cell ID; its `var` index is the original Ensembl gene ID. The Parquet
tables carry the corresponding keys and original source labels.

## QC policy and metrics

`configs/qc.yaml` defines a versioned demonstration policy. Increment
`policy_version` when changing its meaning or thresholds.

| Metric | Definition |
| --- | --- |
| `total_counts` | Sum of unnormalized gene counts in a cell |
| `n_genes_by_counts` | Number of genes with a nonzero count in a cell |
| `mitochondrial_counts` | Sum over source feature names beginning with `MT-` |
| `pct_counts_mito` | `100 * mitochondrial_counts / total_counts` |

For zero-count cells, mitochondrial percentage is undefined (Parquet null),
and `zero_counts` always excludes the cell. A missing mitochondrial prefix
match fails processing rather than silently reporting zero percentages.
The prefix is a source-specific annotation convention, not a general-purpose
mitochondrial gene annotation service.

| Exclusion reason | Default condition |
| --- | --- |
| `zero_counts` | `total_counts == 0` |
| `low_genes` | `n_genes_by_counts < 200` |
| `high_genes` | `n_genes_by_counts >= 2500` |
| `high_mito` | `pct_counts_mito >= 5` |

`qc_pass` is true only when no reason applies. Reasons are semicolon-separated
in the order above; each reason also has a Boolean column. Multiple reasons
can apply, so reason counts may overlap. High gene counts are an exclusion
heuristic here, not a doublet classification. This pipeline does not normalize,
cluster, infer biological labels, or perform differential expression.

## Publication and replay

Release IDs hash the canonical dataset manifest, QC policy, pipeline module
checksums, and relevant runtime versions. Configuration ordering and paths do
not affect the ID. Changing inputs, policy, pipeline code, or runtime creates
a separate release. The JSON manifest contains no time-dependent fields;
timestamps and the best-effort Git commit are recorded in separate run audits.
Module checksums identify the actual code even when it is uncommitted.

Before publication, Parquet roundtrips must preserve data types and values,
row counts and identifiers must reconcile, pass/fail counts must reconcile,
and the AnnData roundtrip must preserve sparse counts and both matrix axes.
Output checksums are verified before the staging directory is renamed to its
final release directory. Only then is `current.json` replaced atomically.
The local implementation supports a single writer; distributed concurrency
control is deferred to cloud orchestration.

Rerunning the same command verifies the source and existing output checksums
and reuses the release. Each invocation records success or the Python failure
in `artifacts/runs/`. Audit timestamps can differ even though published outputs
remain unchanged. Abrupt process termination can leave staging directories;
these are never referenced by `current.json`.

## Recovery

1. Inspect the command error and the failed run record in `artifacts/runs/`.
2. For a source checksum mismatch, preserve the suspect file separately and
   compare it with the pinned source. Restore the expected file or reacquire
   it after removing the suspect cached copy. Do not change the checksum just
   to make validation pass.
3. For invalid QC configuration or missing annotations, correct the policy,
   increment its version if its meaning changed, and rerun.
4. For corrupted published outputs, preserve the suspect release separately,
   restore it from a verified copy or regenerate after moving it out of
   `releases/`. Reuse checks fail rather than overwrite damaged releases.
5. Read `current.json` only after a successful run. Confirm the new release's
   checksum validation succeeds before consuming its outputs.

The source file is immutable during processing. Failures during a new release
do not replace a previous successful pointer. External modification of an
already published release is detected on replay; keep release files read-only
in normal operation. Cloud access controls and lifecycle policies come later.

## Verification

The test fixture exercises exact QC boundaries, zero counts, overlapping
reasons, original gene IDs, invalid count values, invalid policy, checksum
failures, Parquet/AnnData roundtrips, policy changes, idempotent replay, and
preservation of the prior release when validation fails.

For the pinned PBMC3k source and default policy, the measured output is:

- 2,700 cells and 32,738 features retained in the exported matrices/tables.
- 2,638 cells pass QC; 62 fail.
- 5 cells fail the upper gene threshold; 57 fail mitochondrial percentage.
- No cells fail minimum genes or zero counts; 6,390,631 input counts preserved.

Relevant upstream references:

- [Scanpy PBMC3k loader](https://scanpy.readthedocs.io/en/1.11.x/generated/scanpy.datasets.pbmc3k.html).
- [Scanpy QC metric definitions](https://scanpy.readthedocs.io/en/1.11.x/generated/scanpy.pp.calculate_qc_metrics.html).
