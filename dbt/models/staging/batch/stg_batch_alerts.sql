-- The alert queue of each day the batch ran: account-days the rules alerted or the model ranked
-- within the daily budget, hubs left out (ADR-0020). Days that are not complete are left out.
with alerts as (
    {{ batch_files('read_parquet', 'alerts.parquet', {
        'alert_id': 'VARCHAR',
        'day': 'DATE',
        'account_key': 'VARCHAR',
        'sources': 'VARCHAR[]',
        'score': 'DOUBLE',
        'rank': 'BIGINT',
        'transaction_ids': 'BIGINT[]',
    }) }}
)

select
    alert_id,
    day,
    account_key,
    sources,
    list_contains(sources, 'model') as by_model,
    len(list_filter(sources, s -> s <> 'model')) > 0 as by_rules,
    score,
    rank,
    transaction_ids
from alerts
where day in (select day from {{ ref('stg_batch_days') }})
