-- One row per rule and complete day of the batch: the rule's alerts, the account-days they fell
-- on, how many held laundering (in hindsight) and how many reached the queue, which leaves the
-- hub accounts out.
with account_days as (
    select day, rule_id, account_key, count(*) as alerts
    from {{ ref('stg_batch_rule_alerts') }}
    group by all
),

laundering as (
    select distinct day, account_key
    from {{ ref('batch_laundering_account_days') }}
)

select
    r.day,
    d.phase,
    r.rule_id,
    sum(r.alerts) as alerts,
    count(*) as account_days,
    count(l.account_key) as account_days_with_laundering,
    count(l.account_key) / count(*) as hit_rate,
    count(q.alert_id) as account_days_in_queue
from account_days as r
join {{ ref('stg_batch_days') }} as d using (day)
left join laundering as l using (day, account_key)
left join {{ ref('fct_batch_alerts') }} as q using (day, account_key)
group by all
