select
    t.*,
    t.amount_paid * paid_fx.usd_per_unit as amount_paid_usd,
    t.amount_received * received_fx.usd_per_unit as amount_received_usd,
    t.sender_account_key = t.receiver_account_key as is_self_transfer,
    t.payment_currency <> t.receiving_currency as is_cross_currency
from {{ ref('stg_transactions') }} as t
inner join {{ ref('fx_rates_usd') }} as paid_fx on paid_fx.currency = t.payment_currency
inner join {{ ref('fx_rates_usd') }} as received_fx on received_fx.currency = t.receiving_currency
