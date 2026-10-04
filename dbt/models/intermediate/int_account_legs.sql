-- Each transaction seen from both accounts: the sender's outgoing leg and the receiver's incoming
-- leg, each in its own currency. A self-transfer gives the same account one leg of each kind.
select
    transaction_id,
    transacted_at,
    sender_account_key as account_key,
    sender_bank_id as bank_id,
    sender_account_id as account_id,
    'out' as direction,
    receiver_account_key as counterparty_account_key,
    amount_paid as amount,
    payment_currency as currency,
    amount_paid_usd as amount_usd,
    payment_format
from {{ ref('int_transactions_usd') }}

union all

select
    transaction_id,
    transacted_at,
    receiver_account_key,
    receiver_bank_id,
    receiver_account_id,
    'in',
    sender_account_key,
    amount_received,
    receiving_currency,
    amount_received_usd,
    payment_format
from {{ ref('int_transactions_usd') }}
