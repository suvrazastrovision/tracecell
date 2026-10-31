select *
from {{ ref('stg_cells') }}
where total_counts < 0
    or n_genes_by_counts < 0
    or mitochondrial_counts < 0
    or mitochondrial_counts > total_counts
    or qc_pass != (exclusion_reasons = '')
    or qc_pass != (not (zero_counts or low_genes or high_genes or high_mito))
    or (total_counts = 0 and (not zero_counts or pct_counts_mito is not null))
    or (total_counts > 0 and (zero_counts or pct_counts_mito is null))
    or pct_counts_mito < 0
    or pct_counts_mito > 100
