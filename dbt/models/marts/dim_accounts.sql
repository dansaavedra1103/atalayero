select
    account_key,
    any_value(bank_id) as bank_id,
    any_value(account_id) as account_id,
    min(transacted_at) as first_transacted_at,
    max(transacted_at) as last_transacted_at,
    count(*) filter (where direction = 'out') as txns_out,
    count(*) filter (where direction = 'in') as txns_in,
    coalesce(sum(amount_usd) filter (where direction = 'out'), 0) as amount_out_usd,
    coalesce(sum(amount_usd) filter (where direction = 'in'), 0) as amount_in_usd,
    count(distinct counterparty_account_key) as counterparties
from {{ ref('int_account_legs') }}
group by account_key
