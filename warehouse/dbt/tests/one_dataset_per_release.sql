select count(*) as dataset_count
from {{ ref('stg_dataset_summary') }}
having count(*) != 1
