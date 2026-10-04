-- Paid and received amounts must agree once converted to US Dollar. Observed on the full data:
-- at most 0.75% apart for amounts of 1 USD or more; below that, the source rounds to cents.
select transaction_id, amount_paid_usd, amount_received_usd
from {{ ref('int_transactions_usd') }}
where abs(amount_paid_usd - amount_received_usd) > 0.01 * amount_paid_usd + 0.05
