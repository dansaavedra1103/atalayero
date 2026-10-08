-- Every rule alert of each day the batch ran, hubs included (ADR-0020). Days that are not
-- complete are left out.
with alerts as (
    {{ batch_files('read_parquet', 'rule_alerts.parquet', {
        'alert_id': 'VARCHAR',
        'rule_id': 'VARCHAR',
        'rule_version': 'VARCHAR',
        'account_key': 'VARCHAR',
        'triggered_at': 'TIMESTAMP',
        'transaction_id': 'BIGINT',
        'value': 'DOUBLE',
    }) }}
)

select
    alert_id,
    rule_id,
    rule_version,
    account_key,
    triggered_at,
    triggered_at::date as day,
    transaction_id,
    value
from alerts
where triggered_at::date in (select day from {{ ref('stg_batch_days') }})
