with cohort_counts as (
    select release_id, count(*) as cells_pass
    from {{ ref('mart_cohort_cells') }}
    group by 1
)
select metrics.release_id
from {{ ref('mart_dataset_qc') }} as metrics
left join cohort_counts as cohort on metrics.release_id = cohort.release_id
where metrics.cells_pass != coalesce(cohort.cells_pass, 0)
