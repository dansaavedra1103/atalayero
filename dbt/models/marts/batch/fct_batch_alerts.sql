-- Each alert of the batch's queue with what it turned out to be, in hindsight: whether its
-- account-day holds laundering. It stays in the warehouse; the serving database gets only the
-- KPIs built from it (ADR-0020).
with laundering as (
    select distinct day, account_key
    from {{ ref('batch_laundering_account_days') }}
)

select
    a.alert_id,
    a.day,
    d.phase,
    a.account_key,
    a.by_model,
    a.by_rules,
    a.score,
    a.rank,
    l.account_key is not null as has_laundering
from {{ ref('stg_batch_alerts') }} as a
join {{ ref('stg_batch_days') }} as d using (day)
left join laundering as l using (day, account_key)
