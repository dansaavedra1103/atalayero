-- Every transaction of a documented attempt is labelled as laundering in the transactions file.
select a.attempt_id, a.transaction_id
from {{ ref('stg_laundering_attempts') }} as a
inner join {{ ref('stg_transactions') }} as t using (transaction_id)
where not t.is_laundering
