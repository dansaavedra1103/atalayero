-- The KPIs count what the batch wrote: the queue and the rule alerts its manifests report.
select d.day
from {{ ref('stg_batch_days') }} as d
left join {{ ref('kpi_daily') }} as k using (day)
left join (
    select day, sum(alerts) as rule_alerts from {{ ref('kpi_rules') }} group by day
) as r using (day)
where k.alerts is distinct from d.alerts
    or coalesce(r.rule_alerts, 0) <> d.rule_alerts
