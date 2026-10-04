select
    transaction_id,
    "timestamp" as transacted_at,
    from_bank as sender_bank_id,
    from_account as sender_account_id,
    from_bank || ':' || from_account as sender_account_key,
    to_bank as receiver_bank_id,
    to_account as receiver_account_id,
    to_bank || ':' || to_account as receiver_account_key,
    amount_paid,
    payment_currency,
    amount_received,
    receiving_currency,
    lower(replace(payment_format, ' ', '_')) as payment_format,
    is_laundering
from {{ source('raw', 'transactions') }}
