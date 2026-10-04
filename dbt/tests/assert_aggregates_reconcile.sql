-- Daily activity, edges and accounts must account for every transaction and every US Dollar.
with transactions as (
    select count(*) as n, sum(amount_paid_usd) as paid, sum(amount_received_usd) as received
    from {{ ref('int_transactions_usd') }}
),

aggregates as (
    select 'daily activity out' as check_name, sum(txns_out) as n, sum(amount_out_usd) as usd, 'paid' as side
    from {{ ref('int_account_daily_activity') }}
    union all
    select 'daily activity in', sum(txns_in), sum(amount_in_usd), 'received'
    from {{ ref('int_account_daily_activity') }}
    union all
    select 'edges', sum(transactions), sum(amount_usd), 'paid'
    from {{ ref('int_account_edges') }}
    union all
    select 'accounts out', sum(txns_out), sum(amount_out_usd), 'paid'
    from {{ ref('dim_accounts') }}
    union all
    select 'accounts in', sum(txns_in), sum(amount_in_usd), 'received'
    from {{ ref('dim_accounts') }}
)

select a.check_name, a.n, t.n as expected_n, a.usd,
    case a.side when 'paid' then t.paid else t.received end as expected_usd
from aggregates as a
cross join transactions as t
where a.n <> t.n
   -- floating-point sums over millions of rows: allow a relative error of 1e-9
   or abs(a.usd - expected_usd) > 1e-9 * abs(expected_usd) + 0.01
