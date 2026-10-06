# Scatter-gather (`scatter_gather`)

## What it is
One account spreads money over several intermediaries (scatter), and those intermediaries
then send it on to a single final account (gather). The intermediaries hide the direct link
between the first and the last account.

## How it shows in the transactions
- A fan-out from a source to several intermediaries, then a fan-in from the same
  intermediaries to one destination.
- Each intermediary receives and pays out about the same amount, within a short time.
- The source and the destination may never transact directly.

## What to check
- `get_graph_neighborhood` with `hops=2`: several counterparties of the alerted account that
  all deal with the same second-hop account.
- `get_transactions` on the intermediaries: do they forward what they received?
- Decide where the alerted account sits: the source, an intermediary or the destination.

## Easily confused with
A company paying several subcontractors who all pay the same supplier; or many customers of
one payment processor. Look for recurrence and amounts that fit a business.

## Sources
Pattern defined in Altman et al., arXiv:2306.16424. Funds dispersed among several personal
accounts before being concentrated again in business accounts or sent abroad are described
in GAFILAT, *Informe de Tipologías Regionales de LA/FT 2025* (mule accounts).
