-- Every share in the batch KPIs lies between 0 and 1.
select 'kpi_daily' as model, day, false_positive_share as share
from {{ ref('kpi_daily') }}
where false_positive_share not between 0 and 1 or detection_rate not between 0 and 1
union all
select 'kpi_rules', day, hit_rate from {{ ref('kpi_rules') }} where hit_rate not between 0 and 1
union all
select 'kpi_typologies', day, detection_rate
from {{ ref('kpi_typologies') }}
where detection_rate not between 0 and 1
