-- One row per complete day of the batch: its queue, how much of it held laundering, and how much
-- of the day's laundering it covered. False positives and detection use the dataset's labels,
-- which no monitoring team has at alert time. The warm-up day has no queue.
with laundering as (
    select
        l.day,
        l.transaction_id,
        bool_or(a.alert_id is not null) as detected
    from {{ ref('batch_laundering_account_days') }} as l
    left join {{ ref('fct_batch_alerts') }} as a using (day, account_key)
    group by all
),

queue as (
    select
        day,
        count(*) as alerts,
        count(*) filter (where by_model) as model_alerts,
        count(*) filter (where by_rules) as rule_alerts,
        count(*) filter (where has_laundering) as alerts_with_laundering
    from {{ ref('fct_batch_alerts') }}
    group by day
),

covered as (
    select
        day,
        count(*) as laundering_transactions,
        count(*) filter (where detected) as laundering_detected
    from laundering
    group by day
)

select
    d.day,
    d.phase,
    d.transactions,
    q.alerts,
    q.model_alerts,
    q.rule_alerts,
    q.alerts_with_laundering,
    1 - q.alerts_with_laundering / q.alerts as false_positive_share,
    coalesce(c.laundering_transactions, 0) as laundering_transactions,
    case when d.alerts is not null then coalesce(c.laundering_detected, 0) end
        as laundering_detected,
    case when d.alerts is not null then c.laundering_detected / c.laundering_transactions end
        as detection_rate,
    d.drift_detected,
    d.champion_family,
    d.champion_version
from {{ ref('stg_batch_days') }} as d
left join queue as q using (day)
left join covered as c using (day)
