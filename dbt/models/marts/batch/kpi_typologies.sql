-- One row per typology and complete day with a queue: the day's laundering transactions of that
-- typology (`untyped` for laundering outside every documented attempt) and how many the queue
-- covered, in hindsight.
with laundering as (
    select
        l.day,
        l.transaction_id,
        coalesce(l.typology, l.label_group) as typology,
        bool_or(a.alert_id is not null) as detected
    from {{ ref('batch_laundering_account_days') }} as l
    left join {{ ref('fct_batch_alerts') }} as a using (day, account_key)
    group by all
)

select
    l.day,
    d.phase,
    l.typology,
    count(*) as laundering_transactions,
    count(*) filter (where l.detected) as laundering_detected,
    count(*) filter (where l.detected) / count(*) as detection_rate
from laundering as l
join {{ ref('stg_batch_days') }} as d using (day)
where d.alerts is not null
group by all
