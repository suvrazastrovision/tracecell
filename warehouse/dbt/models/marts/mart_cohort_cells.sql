-- Eligibility is QC-only; no biological cell-type or donor labels are inferred.
select *
from {{ ref('stg_cells') }}
where qc_pass
