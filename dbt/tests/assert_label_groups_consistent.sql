select *
from {{ ref('fct_laundering_labels') }}
where (label_group = 'patterned' and (not is_laundering or attempt_id is null or typology is null))
   or (label_group = 'untyped' and (not is_laundering or attempt_id is not null))
   or (label_group = 'clean' and (is_laundering or attempt_id is not null))
