select
    account_key,
    transacted_at::date as activity_date,
    count(*) filter (where direction = 'out') as txns_out,
    count(*) filter (where direction = 'in') as txns_in,
    coalesce(sum(amount_usd) filter (where direction = 'out'), 0) as amount_out_usd,
    coalesce(sum(amount_usd) filter (where direction = 'in'), 0) as amount_in_usd,
    count(distinct counterparty_account_key) filter (where direction = 'out') as counterparties_out,
    count(distinct counterparty_account_key) filter (where direction = 'in') as counterparties_in
from {{ ref('int_account_legs') }}
group by all
