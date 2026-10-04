select
    attempt_id,
    typology,
    description as attempt_description,
    transaction_id
from {{ source('raw', 'laundering_attempts') }}
