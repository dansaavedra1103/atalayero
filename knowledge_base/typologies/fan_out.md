# Fan-out (`fan_out`)

## What it is
One account sends money to many different accounts in a short time. It spreads a large sum
over several receivers, so that each piece looks small and the trail splits. In laundering
terms it belongs to layering: the money is moved away from where it was placed.

## How it shows in the transactions
- A burst of outgoing transfers from one account to two or more new or rarely used
  counterparties, often within hours.
- The outgoing amounts add up to roughly what the account received shortly before.
- Similar amounts, or amounts just below a round figure, sent to each receiver.
- The receivers have little other activity, and may pass the money on themselves.

## What to check
- `get_transactions` on the alerted day: count the distinct receivers and compare the total
  sent with what came in during the previous days.
- `get_graph_neighborhood` with `hops=2`: do the receivers forward the money onwards?
- `get_account_profile`: is this burst unusual for the account's history?

## Easily confused with
Payroll, supplier runs and other regular disbursements: same receivers every period, amounts
that fit a business, and a long history of the same pattern.

## Sources
Pattern defined in Altman et al., *Realistic Synthetic Financial Transactions for Anti-Money
Laundering Models* (arXiv:2306.16424). Dispersal of funds across many personal and business
accounts is a recurring technique in GAFILAT, *Informe de Tipologías Regionales de LA/FT 2025*
(use of third parties and mule accounts).
