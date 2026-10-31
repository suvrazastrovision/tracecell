select
    '{{ pbmc_release_id() }}' as release_id,
    source_summary.*
from {{ source('pbmc_raw', 'dataset_summary') }} as source_summary
