select
    t.transaction_id,
    t.is_laundering,
    case
        when a.attempt_id is not null then 'patterned'
        when t.is_laundering then 'untyped'
        else 'clean'
    end as label_group,
    a.attempt_id,
    a.typology
from {{ ref('stg_transactions') }} as t
left join {{ ref('stg_laundering_attempts') }} as a using (transaction_id)
