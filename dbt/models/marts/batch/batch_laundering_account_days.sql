{{ config(materialized='ephemeral') }}
-- Each account a laundering transaction touches, on its day, with the transaction's label: what
-- an alert on that account-day would have covered. Hindsight only: labels are the answer.
with laundering as (
    select
        t.transaction_id,
        t.transacted_at::date as day,
        t.sender_account_key,
        t.receiver_account_key,
        l.label_group,
        l.typology
    from {{ ref('fct_transactions') }} as t
    join {{ ref('fct_laundering_labels') }} as l using (transaction_id)
    where l.is_laundering
        and t.transacted_at::date in (select day from {{ ref('stg_batch_days') }})
)

select transaction_id, day, sender_account_key as account_key, label_group, typology
from laundering
union
select transaction_id, day, receiver_account_key, label_group, typology
from laundering
