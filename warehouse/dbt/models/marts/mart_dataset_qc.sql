with cell_metrics as (
    select
        release_id, dataset_id, dataset_version, policy_id, policy_version,
        count(*) as cells_total,
        sum(case when qc_pass then 1 else 0 end) as cells_pass,
        sum(case when not qc_pass then 1 else 0 end) as cells_fail,
        sum(total_counts) as total_counts,
        sum(case when zero_counts then 1 else 0 end) as cells_zero_counts,
        sum(case when low_genes then 1 else 0 end) as cells_low_genes,
        sum(case when high_genes then 1 else 0 end) as cells_high_genes,
        sum(case when high_mito then 1 else 0 end) as cells_high_mito,
        avg(1.0 * total_counts) as mean_counts_per_cell,
        avg(1.0 * n_genes_by_counts) as mean_genes_per_cell,
        avg(pct_counts_mito) as mean_pct_counts_mito
    from {{ ref('stg_cells') }}
    group by 1, 2, 3, 4, 5
), feature_metrics as (
    select release_id, dataset_id, dataset_version,
        count(*) as features_total,
        sum(case when is_mitochondrial then 1 else 0 end) as mitochondrial_features
    from {{ ref('stg_genes') }}
    group by 1, 2, 3
)
select cells.*, genes.features_total, genes.mitochondrial_features,
    1.0 * cells.cells_pass / cells.cells_total as qc_pass_fraction
from cell_metrics as cells
join feature_metrics as genes
    on cells.release_id = genes.release_id
    and cells.dataset_id = genes.dataset_id
    and cells.dataset_version = genes.dataset_version
