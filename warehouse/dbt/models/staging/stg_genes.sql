select
    '{{ pbmc_release_id() }}' as release_id,
    source_genes.*
from {{ source('pbmc_raw', 'genes') }} as source_genes
