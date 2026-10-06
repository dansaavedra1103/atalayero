# Cycle (`cycle`)

## What it is
Money leaves an account and, after passing through one or more intermediaries, comes back
to the same account. The round trip creates a record of payments that looks like trade or
lending, while the funds end up where they started.

## How it shows in the transactions
- A chain of transfers in time order, A → B → … → A, within days.
- The amount stays close to the original at every hop, minus small fees or rounding.
- Intermediaries that receive and pay out almost the same amount quickly.

## What to check
- `get_graph_neighborhood`: it lists cycles that bring money back to the account in time
  order, with their transaction IDs and amounts.
- `get_transactions` for each intermediary (pass its `account_key`): do they forward almost
  everything they receive?

## Easily confused with
Two businesses that trade both ways (a supplier that is also a customer), or loans that are
repaid. Those show round trips too, but with a long history, regular timing, and amounts
that differ in each direction.

## Sources
Pattern defined in Altman et al., arXiv:2306.16424. Triangulation of payments between
related companies to give funds an appearance of legitimacy appears in GAFILAT,
*Informe de Tipologías Regionales de LA/FT 2025* (trade-based laundering).
