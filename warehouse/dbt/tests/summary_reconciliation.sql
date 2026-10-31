-- Independently aggregated warehouse metrics must match the QC producer.
select summary.*
from {{ ref('stg_dataset_summary') }} as summary
left join {{ ref('mart_dataset_qc') }} as metrics
    on summary.release_id = metrics.release_id
    and summary.dataset_id = metrics.dataset_id
    and summary.dataset_version = metrics.dataset_version
    and summary.policy_id = metrics.policy_id
    and summary.policy_version = metrics.policy_version
where metrics.release_id is null
{% for metric in ['cells_total', 'features_total', 'cells_pass', 'cells_fail', 'total_counts', 'cells_zero_counts', 'cells_low_genes', 'cells_high_genes', 'cells_high_mito'] %}
    or summary.{{ metric }} != metrics.{{ metric }}
{% endfor %}
