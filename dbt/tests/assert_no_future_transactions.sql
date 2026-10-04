select transaction_id, transacted_at
from {{ ref('stg_transactions') }}
where transacted_at > now()::timestamp
