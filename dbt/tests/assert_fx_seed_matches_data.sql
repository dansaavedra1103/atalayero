-- The committed rates must still match the data: for every currency with enough cross-currency
-- transactions against US Dollar, the implied median rate is within 0.1% of the seed.
-- (The test fixture has too few such transactions, so this bites on the full dataset.)
with implied as (
    select payment_currency as currency, amount_received / amount_paid as usd_per_unit
    from {{ ref('stg_transactions') }}
    where receiving_currency = 'US Dollar' and payment_currency <> 'US Dollar' and amount_paid > 0
    union all
    select receiving_currency, amount_paid / amount_received
    from {{ ref('stg_transactions') }}
    where payment_currency = 'US Dollar' and receiving_currency <> 'US Dollar' and amount_received > 0
),

medians as (
    select currency, median(usd_per_unit) as usd_per_unit, count(*) as observations
    from implied
    group by currency
    having count(*) >= 30
)

select m.currency, m.usd_per_unit as implied, s.usd_per_unit as seed
from medians as m
inner join {{ ref('fx_rates_usd') }} as s using (currency)
where abs(m.usd_per_unit / s.usd_per_unit - 1) > 0.001
