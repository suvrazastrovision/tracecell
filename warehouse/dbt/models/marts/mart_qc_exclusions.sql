{% for reason in ['zero_counts', 'low_genes', 'high_genes', 'high_mito'] %}
select
    release_id, dataset_id, dataset_version, policy_id, policy_version,
    '{{ reason }}' as exclusion_reason,
    sum(case when {{ reason }} then 1 else 0 end) as excluded_cells
from {{ ref('stg_cells') }}
group by 1, 2, 3, 4, 5
{% if not loop.last %}union all{% endif %}
{% endfor %}
