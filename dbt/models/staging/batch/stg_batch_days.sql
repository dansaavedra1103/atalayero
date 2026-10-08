-- The complete days of the daily batch, from their manifests (ADR-0020). A day without a
-- manifest is still running or failed, and every batch model leaves it out.
with manifests as (
    {{ batch_files('read_json', 'manifest.json', {
        'day': 'DATE',
        'phase': 'VARCHAR',
        'transactions': 'BIGINT',
        'rules': 'JSON',
        'rule_alerts': 'BIGINT',
        'champion': 'STRUCT(version VARCHAR, family VARCHAR)',
        'alerts': 'BIGINT',
        'drift_detected': 'BOOLEAN',
        'written_at': 'TIMESTAMPTZ',
    }) }}
)

select
    day,
    phase,
    transactions,
    rule_alerts,
    alerts,
    champion.family as champion_family,
    champion.version as champion_version,
    drift_detected,
    written_at
from manifests
