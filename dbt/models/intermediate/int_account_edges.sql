select
    sender_account_key,
    receiver_account_key,
    count(*) as transactions,
    sum(amount_paid_usd) as amount_usd,
    min(transacted_at) as first_transacted_at,
    max(transacted_at) as last_transacted_at
from {{ ref('int_transactions_usd') }}
group by all
