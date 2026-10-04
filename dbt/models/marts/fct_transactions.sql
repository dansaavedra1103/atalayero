-- No labels here: they live in fct_laundering_labels, so consumers that must not see the answer
-- (the phase 3 agent's tools) can read transactions alone.
select * exclude (is_laundering)
from {{ ref('int_transactions_usd') }}
