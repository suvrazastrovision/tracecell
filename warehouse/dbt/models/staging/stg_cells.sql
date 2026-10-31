select
    '{{ pbmc_release_id() }}' as release_id,
    source_cells.*
from {{ source('pbmc_raw', 'cells') }} as source_cells
